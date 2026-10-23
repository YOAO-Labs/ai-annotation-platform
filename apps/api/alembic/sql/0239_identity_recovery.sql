-- Native transactional ownership for account recovery, open registration and
-- credential mail delivery.
CREATE SCHEMA aap_identity_recovery_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_recovery_v1 FROM PUBLIC;
-- aap-statement-break
ALTER TABLE public.password_reset_tokens RENAME COLUMN token TO token_hash;
-- aap-statement-break
ALTER TABLE public.email_verification_tokens RENAME COLUMN token TO token_hash;
-- aap-statement-break
ALTER INDEX public.ix_password_reset_tokens_token RENAME TO ix_password_reset_tokens_token_hash;
-- aap-statement-break
ALTER INDEX public.ix_email_verification_tokens_token RENAME TO ix_email_verification_tokens_token_hash;
-- aap-statement-break
ALTER TABLE public.password_reset_tokens ADD COLUMN issued_email varchar(255);
-- aap-statement-break
ALTER TABLE public.email_verification_tokens ADD COLUMN issued_email varchar(255);
-- aap-statement-break
UPDATE public.password_reset_tokens AS token
SET token_hash = pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(token.token_hash, 'UTF8')), 'hex'
    ),
    issued_email = account.email
FROM public.users AS account
WHERE account.id = token.user_id;
-- aap-statement-break
UPDATE public.email_verification_tokens AS token
SET token_hash = pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(token.token_hash, 'UTF8')), 'hex'
    ),
    issued_email = account.email
FROM public.users AS account
WHERE account.id = token.user_id;
-- aap-statement-break
ALTER TABLE public.password_reset_tokens ALTER COLUMN issued_email SET NOT NULL;
-- aap-statement-break
ALTER TABLE public.email_verification_tokens ALTER COLUMN issued_email SET NOT NULL;
-- aap-statement-break
ALTER TABLE public.password_reset_tokens
    ADD CONSTRAINT ck_password_reset_tokens_hash_hex
    CHECK (token_hash COLLATE "C" ~ '^[0-9a-f]{64}$');
-- aap-statement-break
ALTER TABLE public.email_verification_tokens
    ADD CONSTRAINT ck_email_verification_tokens_hash_hex
    CHECK (token_hash COLLATE "C" ~ '^[0-9a-f]{64}$');
-- aap-statement-break
CREATE TABLE public.identity_recovery_operation_receipts (
    operation_id uuid PRIMARY KEY,
    operation text NOT NULL CHECK (operation IN (
        'register_open', 'request_password_reset', 'resend_email_verification',
        'consume_password_reset', 'consume_email_verification'
    )),
    actor_id uuid,
    request_fingerprint text NOT NULL CHECK (
        request_fingerprint COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    derivation_id text NOT NULL CHECK (derivation_id COLLATE "C" ~ '^[0-9a-f]{64}$'),
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    audit_id bigint,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE INDEX ix_identity_recovery_operation_receipts_actor
    ON public.identity_recovery_operation_receipts (actor_id, created_at DESC)
    WHERE actor_id IS NOT NULL;
-- aap-statement-break
COMMENT ON TABLE public.identity_recovery_operation_receipts IS
    'Identity recovery idempotency results; operation IDs are not retargetable after User deletion.';
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_recovery_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_credential_mail_outbox (
    id uuid PRIMARY KEY,
    actor_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    challenge_id uuid NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('password_reset', 'email_verification')),
    email text NOT NULL CHECK (pg_catalog.char_length(email) BETWEEN 3 AND 255),
    frontend_base_url text NOT NULL,
    token_hash text NOT NULL CHECK (token_hash COLLATE "C" ~ '^[0-9a-f]{64}$'),
    token_ciphertext text NOT NULL CHECK (pg_catalog.char_length(token_ciphertext) BETWEEN 1 AND 4096),
    encryption_id text NOT NULL CHECK (encryption_id COLLATE "C" ~ '^[0-9a-f]{64}$'),
    expires_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'leased', 'delivered', 'rejected', 'cancelled', 'expired')
    ),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    available_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    worker_id uuid,
    lease_token uuid,
    lease_seconds smallint,
    lease_expires_at timestamptz,
    last_reason text CHECK (last_reason IS NULL OR last_reason IN (
        'sent', 'not_configured', 'message_invalid', 'tls_failed', 'auth_failed',
        'recipient_rejected', 'sender_rejected', 'data_rejected', 'connect_failed',
        'transport_failed', 'derivation_unavailable'
    )),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (
        (status = 'leased' AND worker_id IS NOT NULL AND lease_token IS NOT NULL
            AND lease_seconds BETWEEN 1 AND 300 AND lease_expires_at IS NOT NULL)
        OR (status <> 'leased' AND worker_id IS NULL AND lease_token IS NULL
            AND lease_seconds IS NULL AND lease_expires_at IS NULL)
    )
);
-- aap-statement-break
CREATE INDEX ix_identity_credential_mail_outbox_pending
    ON public.identity_credential_mail_outbox (available_at, created_at)
    WHERE status IN ('pending', 'leased');
-- aap-statement-break
CREATE INDEX ix_identity_credential_mail_outbox_actor
    ON public.identity_credential_mail_outbox (actor_id, created_at DESC);
-- aap-statement-break
COMMENT ON TABLE public.identity_credential_mail_outbox IS
    'Durable, encrypted credential-mail intents; raw challenge tokens are never stored.';
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_credential_mail_outbox FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_settings_v1.identity_recovery_policy(p_fallbacks jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count integer;
    v_open jsonb;
    v_frontend jsonb;
    v_allow boolean;
    v_url text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_fallbacks) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid recovery fallbacks';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_fallbacks);
    IF v_count <> 2 OR NOT (p_fallbacks ?& ARRAY['allow_open_registration', 'frontend_base_url'])
       OR pg_catalog.jsonb_typeof(p_fallbacks->'allow_open_registration') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_fallbacks->'frontend_base_url') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid recovery fallbacks';
    END IF;

    -- Retain a table-level fence through the caller's account transaction so a
    -- concurrent Settings edit cannot disable registration mid-command.
    LOCK TABLE public.system_settings IN SHARE MODE;
    SELECT setting.value_json INTO v_open
    FROM public.system_settings AS setting
    WHERE setting.key = 'allow_open_registration';
    IF v_open IS NULL OR v_open = 'null'::jsonb THEN
        v_allow := (p_fallbacks->>'allow_open_registration')::boolean;
    ELSIF pg_catalog.jsonb_typeof(v_open) = 'boolean' THEN
        v_allow := (v_open #>> '{}')::boolean;
    ELSE
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid registration setting';
    END IF;

    SELECT setting.value_json INTO v_frontend
    FROM public.system_settings AS setting
    WHERE setting.key = 'frontend_base_url';
    IF v_frontend IS NULL OR v_frontend = 'null'::jsonb THEN
        v_url := p_fallbacks->>'frontend_base_url';
    ELSIF pg_catalog.jsonb_typeof(v_frontend) = 'string' THEN
        v_url := v_frontend #>> '{}';
        IF pg_catalog.btrim(v_url) = '' THEN
            v_url := p_fallbacks->>'frontend_base_url';
        END IF;
    ELSE
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid frontend URL setting';
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'allow_open_registration', v_allow,
        'frontend_base_url', v_url
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_settings_v1.identity_recovery_policy(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_settings_v1.mail_delivery_policy(p_fallbacks jsonb)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count integer;
    v_values jsonb;
    v_host jsonb;
    v_port jsonb;
    v_user jsonb;
    v_password jsonb;
    v_from jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_fallbacks) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid mail fallbacks';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_fallbacks);
    IF v_count <> 5 OR NOT (p_fallbacks ?& ARRAY[
        'smtp_host', 'smtp_port', 'smtp_user', 'smtp_password', 'smtp_from'
    ]) OR pg_catalog.jsonb_typeof(p_fallbacks->'smtp_host') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_fallbacks->'smtp_port') IS DISTINCT FROM 'number'
       OR (p_fallbacks->>'smtp_port') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(p_fallbacks->'smtp_user') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_fallbacks->'smtp_password') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_fallbacks->'smtp_from') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid mail fallbacks';
    END IF;
    IF (p_fallbacks->>'smtp_port')::numeric NOT BETWEEN 0 AND 65535 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid mail port fallback';
    END IF;

    -- A single SELECT gives all five values one PostgreSQL statement snapshot.
    SELECT COALESCE(
        pg_catalog.jsonb_object_agg(setting.key, setting.value_json), '{}'::jsonb
    )
    INTO v_values
    FROM public.system_settings AS setting
    WHERE setting.key IN ('smtp_host', 'smtp_port', 'smtp_user', 'smtp_password', 'smtp_from')
      AND setting.value_json IS NOT NULL
      AND setting.value_json <> 'null'::jsonb;

    v_host := COALESCE(v_values->'smtp_host', p_fallbacks->'smtp_host');
    v_port := COALESCE(v_values->'smtp_port', p_fallbacks->'smtp_port');
    v_user := COALESCE(v_values->'smtp_user', p_fallbacks->'smtp_user');
    v_password := COALESCE(v_values->'smtp_password', p_fallbacks->'smtp_password');
    v_from := COALESCE(v_values->'smtp_from', p_fallbacks->'smtp_from');

    IF pg_catalog.jsonb_typeof(v_host) IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_port) IS DISTINCT FROM 'number'
       OR (v_port #>> '{}') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(v_user) IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_password) IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_from) IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid stored mail settings';
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'smtp_host', v_host #>> '{}',
        'smtp_port', (v_port #>> '{}')::integer,
        'smtp_user', v_user #>> '{}',
        'smtp_password', v_password #>> '{}',
        'smtp_from', v_from #>> '{}'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_settings_v1.mail_delivery_policy(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_identity_recovery_event(
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
    v_details integer;
BEGIN
    IF p_event IS NULL OR p_event NOT IN (
        'register_open', 'consume_password_reset', 'consume_email_verification'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid recovery audit event';
    END IF;
    IF pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object'
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid recovery audit context';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_context);
    IF v_field_count <> 4 OR NOT (p_context ?& ARRAY['method', 'path', 'ip', 'request_id']) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid recovery audit context';
    END IF;
    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid recovery audit actor';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_actor);
    IF v_field_count <> 3 OR NOT (p_actor ?& ARRAY['actor_id', 'email', 'role'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_actor) AS field(name)
           WHERE field.name NOT IN ('actor_id', 'email', 'role')
       )
       OR pg_catalog.jsonb_typeof(p_actor->'actor_id') IS DISTINCT FROM 'string'
       OR (p_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_actor->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_actor->>'email') NOT BETWEEN 1 AND 255
       OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
       OR p_actor->>'role' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid recovery audit actor';
    END IF;
    v_actor_id := (p_actor->>'actor_id')::uuid;
    v_actor_email := p_actor->>'email';
    v_actor_role := p_actor->>'role';

    IF p_event = 'register_open' THEN
        SELECT pg_catalog.count(*)::integer INTO v_details
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object'
           OR v_details <> 4
           OR NOT (p_detail ?& ARRAY[
               'email', 'role', 'method', 'email_verification_required'
           ])
           OR p_detail->>'email' IS DISTINCT FROM v_actor_email
           OR p_detail->>'role' IS DISTINCT FROM v_actor_role
           OR p_detail->>'method' IS DISTINCT FROM 'open_registration'
           OR pg_catalog.jsonb_typeof(p_detail->'email_verification_required') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid registration audit detail';
        END IF;
        v_action := 'user.register';
        v_status_code := 201;
    ELSIF p_event = 'consume_password_reset' THEN
        SELECT pg_catalog.count(*)::integer INTO v_details
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object'
           OR v_details <> 1
           OR p_detail->>'method' IS DISTINCT FROM 'reset_token' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid password recovery audit detail';
        END IF;
        v_action := 'user.password_change';
        v_status_code := 200;
    ELSE
        SELECT pg_catalog.count(*)::integer INTO v_details
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object'
           OR v_details <> 2
           OR NOT (p_detail ?& ARRAY['email', 'result'])
           OR p_detail->>'email' IS DISTINCT FROM v_actor_email
           OR p_detail->>'result' IS DISTINCT FROM 'email_verified' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid email verification audit detail';
        END IF;
        v_action := 'user.register';
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
REVOKE ALL ON FUNCTION aap_audit_v1.record_identity_recovery_event(text, jsonb, jsonb, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_recovery_v1._failure(p_code text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-recovery-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_recovery_v1._failure(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_recovery_v1._mail_failure(p_code text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-recovery-mail-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_recovery_v1._mail_failure(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_recovery_v1._actor_record(p_actor_id uuid)
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
        'role', account.role,
        'is_active', account.is_active,
        'credential_generation', account.credential_generation,
        'email_verified_at', account.email_verified_at
    )
    FROM public.users AS account
    WHERE account.id = p_actor_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_recovery_v1._actor_record(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_recovery_v1._token_record(
    p_operation text,
    p_token_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_result jsonb;
BEGIN
    IF p_operation = 'consume_password_reset' THEN
        SELECT pg_catalog.jsonb_build_object(
            'id', token.id, 'token_hash', token.token_hash,
            'issued_email', token.issued_email, 'expires_at', token.expires_at,
            'used_at', token.used_at
        ) INTO v_result
        FROM public.password_reset_tokens AS token
        WHERE token.id = p_token_id;
    ELSE
        SELECT pg_catalog.jsonb_build_object(
            'id', token.id, 'token_hash', token.token_hash,
            'issued_email', token.issued_email, 'expires_at', token.expires_at,
            'used_at', token.used_at
        ) INTO v_result
        FROM public.email_verification_tokens AS token
        WHERE token.id = p_token_id;
    END IF;
    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_recovery_v1._token_record(text, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_recovery_v1.recovery_policy()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-recovery-policy/v1',
        'policy', 'aap-identity-recovery/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_recovery_v1.recovery_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_recovery_v1.mail_policy()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-recovery-mail-policy/v1',
        'policy', 'aap-identity-recovery-mail/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_recovery_v1.mail_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_recovery_v1.prepare_recovery_command(p_request jsonb)
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
    v_operation_id_text text;
    v_fingerprint text;
    v_derivation_id text;
    v_context jsonb;
    v_config jsonb;
    v_args jsonb;
    v_expire_minutes integer;
    v_email text;
    v_name text;
    v_captcha boolean;
    v_token_hash text;
    v_policy_settings jsonb;
    v_policy jsonb;
    v_actor_id uuid;
    v_actor jsonb;
    v_token_id uuid;
    v_token jsonb;
    v_account jsonb;
    v_claims jsonb;
    v_password_hash text;
    v_eligible boolean := false;
    v_replay jsonb;
    v_receipt_operation text;
    v_receipt_fingerprint text;
    v_receipt_derivation_id text;
    v_receipt_result jsonb;
    v_receipt_actor_id uuid;
    v_user_email text;
    v_user_active boolean;
    v_user_role text;
    v_user_verified_at timestamptz;
    v_user_hash text;
    v_expires_at timestamptz;
    v_used_at timestamptz;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_recovery_v1._failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_field_count <> 8 OR NOT (p_request ?& ARRAY[
        'schema', 'operation', 'operation_id', 'request_fingerprint',
        'derivation_id', 'audit_context', 'config', 'args'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
        WHERE field.name NOT IN (
            'schema', 'operation', 'operation_id', 'request_fingerprint',
            'derivation_id', 'audit_context', 'config', 'args'
        )
    ) OR p_request->>'schema' IS DISTINCT FROM 'aap-identity-recovery-native/v1'
       OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'request_fingerprint') IS DISTINCT FROM 'string'
       OR (p_request->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_request->'derivation_id') IS DISTINCT FROM 'string'
       OR (p_request->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_request->'audit_context') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_request->'config') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_request->'args') IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_recovery_v1._failure('invalid_request');
    END IF;
    v_operation := p_request->>'operation';
    v_operation_id_text := p_request->>'operation_id';
    v_fingerprint := p_request->>'request_fingerprint';
    v_derivation_id := p_request->>'derivation_id';
    v_context := p_request->'audit_context';
    v_config := p_request->'config';
    v_args := p_request->'args';
    IF v_operation NOT IN (
        'register_open', 'request_password_reset', 'resend_email_verification',
        'consume_password_reset', 'consume_email_verification'
    ) OR v_operation_id_text !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN aap_identity_recovery_v1._failure('invalid_request');
    END IF;
    BEGIN
        v_operation_id := v_operation_id_text::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_recovery_v1._failure('invalid_request');
    END;

    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(v_context);
    IF v_field_count <> 4 OR NOT (v_context ?& ARRAY['method', 'path', 'ip', 'request_id'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(v_context) AS field(name)
           WHERE field.name NOT IN ('method', 'path', 'ip', 'request_id')
       ) OR pg_catalog.jsonb_typeof(v_context->'method') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_context->'path') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_context->'ip') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_context->'request_id') NOT IN ('string', 'null')
       OR NOT aap_audit_v1.valid_request_context(v_context) THEN
        RETURN aap_identity_recovery_v1._failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(v_config);
    IF v_field_count <> 4 OR NOT (v_config ?& ARRAY[
        'allow_open_registration', 'email_verification_required',
        'frontend_base_url', 'expire_minutes'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(v_config) AS field(name)
        WHERE field.name NOT IN (
            'allow_open_registration', 'email_verification_required',
            'frontend_base_url', 'expire_minutes'
        )
    ) OR pg_catalog.jsonb_typeof(v_config->'allow_open_registration') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_config->'email_verification_required') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_config->'frontend_base_url') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_config->'expire_minutes') IS DISTINCT FROM 'number'
       OR (v_config->>'expire_minutes') !~ '^[1-9][0-9]*$' THEN
        RETURN aap_identity_recovery_v1._failure('invalid_request');
    END IF;
    BEGIN
        v_expire_minutes := (v_config->>'expire_minutes')::integer;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_identity_recovery_v1._failure('invalid_request');
    END;
    IF v_expire_minutes NOT BETWEEN 1 AND 525600 THEN
        RETURN aap_identity_recovery_v1._failure('invalid_request');
    END IF;

    IF v_operation IN ('register_open', 'request_password_reset') THEN
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(v_args);
        IF v_field_count <> (CASE WHEN v_operation = 'register_open' THEN 3 ELSE 2 END)
           OR NOT (v_args ?& ARRAY['email', 'captcha_verified'])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_args) AS field(name)
               WHERE field.name NOT IN ('email', 'captcha_verified', 'name')
           ) OR pg_catalog.jsonb_typeof(v_args->'email') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_args->>'email') NOT BETWEEN 3 AND 255
           OR pg_catalog.jsonb_typeof(v_args->'captcha_verified') IS DISTINCT FROM 'boolean'
           OR (v_operation = 'register_open' AND (
               NOT (v_args ? 'name')
               OR pg_catalog.jsonb_typeof(v_args->'name') IS DISTINCT FROM 'string'
               OR pg_catalog.char_length(v_args->>'name') NOT BETWEEN 1 AND 100
           )) THEN
            RETURN aap_identity_recovery_v1._failure('invalid_request');
        END IF;
        v_email := pg_catalog.lower(pg_catalog.btrim(v_args->>'email'));
        v_captcha := (v_args->>'captcha_verified')::boolean;
        IF v_operation = 'register_open' THEN
            v_name := v_args->>'name';
        END IF;
    ELSIF v_operation = 'resend_email_verification' THEN
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(v_args);
        IF v_field_count <> 1
           OR NOT (v_args ? 'email')
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_args) AS field(name)
               WHERE field.name <> 'email'
           ) OR pg_catalog.jsonb_typeof(v_args->'email') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_args->>'email') NOT BETWEEN 3 AND 255 THEN
            RETURN aap_identity_recovery_v1._failure('invalid_request');
        END IF;
        v_email := pg_catalog.lower(pg_catalog.btrim(v_args->>'email'));
    ELSE
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(v_args);
        IF v_field_count <> 1
           OR NOT (v_args ? 'token_hash')
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_args) AS field(name)
               WHERE field.name <> 'token_hash'
           ) OR pg_catalog.jsonb_typeof(v_args->'token_hash') IS DISTINCT FROM 'string'
           OR (v_args->>'token_hash') COLLATE "C" !~ '^[0-9a-f]{64}$' THEN
            RETURN aap_identity_recovery_v1._failure('invalid_request');
        END IF;
        v_token_hash := v_args->>'token_hash';
    END IF;

    -- Serialize operation IDs before consulting receipts or locking account data.
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(v_operation_id::text, 2390239)
    );

    v_policy_settings := pg_catalog.jsonb_build_object(
        'allow_open_registration', v_config->'allow_open_registration',
        'frontend_base_url', v_config->'frontend_base_url'
    );
    v_policy := pg_catalog.jsonb_build_object(
        'allow_open_registration', v_policy_settings->'allow_open_registration',
        'email_verification_required', v_config->'email_verification_required',
        'frontend_base_url', v_policy_settings->'frontend_base_url',
        'expire_minutes', v_config->'expire_minutes',
        'password_reset_ttl_seconds', 3600,
        'email_verification_ttl_seconds', 86400
    );

    SELECT receipt.operation, receipt.request_fingerprint,
           receipt.derivation_id, receipt.result, receipt.actor_id
      INTO v_receipt_operation, v_receipt_fingerprint,
           v_receipt_derivation_id, v_receipt_result, v_receipt_actor_id
    FROM public.identity_recovery_operation_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    IF FOUND THEN
        IF v_receipt_operation IS DISTINCT FROM v_operation
           OR v_receipt_fingerprint IS DISTINCT FROM v_fingerprint
           OR v_receipt_derivation_id IS DISTINCT FROM v_derivation_id THEN
            RETURN aap_identity_recovery_v1._failure('operation_conflict');
        END IF;
        v_replay := v_receipt_result;
        v_eligible := true;
        IF v_operation = 'register_open' THEN
            IF pg_catalog.jsonb_typeof(v_replay->'account') IS DISTINCT FROM 'object' THEN
                RETURN aap_identity_recovery_v1._failure('operation_conflict');
            END IF;
            BEGIN
                v_actor_id := (v_replay->'account'->>'id')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RETURN aap_identity_recovery_v1._failure('operation_conflict');
            END;
            SELECT account.email, account.is_active, account.role,
                   account.email_verified_at, account.password_hash
              INTO v_user_email, v_user_active, v_user_role,
                   v_user_verified_at, v_password_hash
            FROM public.users AS account
            WHERE account.id = v_actor_id
            FOR UPDATE;
            IF NOT FOUND OR NOT v_user_active
               OR v_user_email IS DISTINCT FROM v_email THEN
                RETURN aap_identity_recovery_v1._failure('operation_conflict');
            END IF;
            v_account := jsonb_set(
                jsonb_set(v_replay->'account', '{email}', pg_catalog.to_jsonb(v_email), true),
                '{name}', pg_catalog.to_jsonb(v_name), true
            );
            v_replay := jsonb_set(v_replay, '{account}', v_account, true);
            v_actor := aap_identity_recovery_v1._actor_record(v_actor_id);
            IF pg_catalog.jsonb_typeof(v_replay->'claims') = 'object' THEN
                -- The package verifies the submitted password before apply.
                NULL;
            ELSE
                v_password_hash := NULL;
            END IF;
        ELSIF v_receipt_actor_id IS NOT NULL THEN
            PERFORM 1 FROM public.users AS account
            WHERE account.id = v_receipt_actor_id FOR UPDATE;
            IF FOUND THEN
                v_actor := aap_identity_recovery_v1._actor_record(v_receipt_actor_id);
            END IF;
        END IF;
    ELSE
        v_replay := NULL;
        IF v_operation = 'register_open' THEN
            v_policy_settings := aap_settings_v1.identity_recovery_policy(
                pg_catalog.jsonb_build_object(
                    'allow_open_registration', v_config->'allow_open_registration',
                    'frontend_base_url', v_config->'frontend_base_url'
                )
            );
            v_policy := jsonb_set(
                jsonb_set(v_policy, '{allow_open_registration}',
                    v_policy_settings->'allow_open_registration', true),
                '{frontend_base_url}', v_policy_settings->'frontend_base_url', true
            );
            IF NOT (v_policy_settings->>'allow_open_registration')::boolean THEN
                RETURN aap_identity_recovery_v1._failure('registration_disabled');
            END IF;
            IF NOT v_captcha THEN
                RETURN aap_identity_recovery_v1._failure('captcha_required');
            END IF;
            IF EXISTS (SELECT 1 FROM public.users AS account WHERE account.email = v_email) THEN
                RETURN aap_identity_recovery_v1._failure('email_exists');
            END IF;
            v_eligible := true;
        ELSIF v_operation = 'request_password_reset' THEN
            IF NOT v_captcha THEN
                RETURN aap_identity_recovery_v1._failure('captcha_required');
            END IF;
            SELECT account.id, account.email, account.is_active,
                   account.role, account.email_verified_at
              INTO v_actor_id, v_user_email, v_user_active,
                   v_user_role, v_user_verified_at
            FROM public.users AS account
            WHERE account.email = v_email;
            IF FOUND THEN
                SELECT account.email, account.is_active, account.role,
                       account.email_verified_at
                  INTO v_user_email, v_user_active, v_user_role, v_user_verified_at
                FROM public.users AS account
                WHERE account.id = v_actor_id
                FOR UPDATE;
                IF FOUND AND v_user_email = v_email AND v_user_active THEN
                    v_policy_settings := aap_settings_v1.identity_recovery_policy(
                        pg_catalog.jsonb_build_object(
                            'allow_open_registration', v_config->'allow_open_registration',
                            'frontend_base_url', v_config->'frontend_base_url'
                        )
                    );
                    v_policy := jsonb_set(
                        jsonb_set(v_policy, '{allow_open_registration}',
                            v_policy_settings->'allow_open_registration', true),
                        '{frontend_base_url}', v_policy_settings->'frontend_base_url', true
                    );
                    v_actor := aap_identity_recovery_v1._actor_record(v_actor_id);
                    v_eligible := true;
                ELSE
                    v_actor_id := NULL;
                END IF;
            END IF;
        ELSIF v_operation = 'resend_email_verification' THEN
            SELECT account.id INTO v_actor_id
            FROM public.users AS account
            WHERE account.email = v_email;
            IF FOUND THEN
                SELECT account.email, account.is_active, account.role,
                       account.email_verified_at
                  INTO v_user_email, v_user_active, v_user_role, v_user_verified_at
                FROM public.users AS account
                WHERE account.id = v_actor_id
                FOR UPDATE;
                IF FOUND AND v_user_email = v_email AND v_user_verified_at IS NULL THEN
                    v_policy_settings := aap_settings_v1.identity_recovery_policy(
                        pg_catalog.jsonb_build_object(
                            'allow_open_registration', v_config->'allow_open_registration',
                            'frontend_base_url', v_config->'frontend_base_url'
                        )
                    );
                    v_policy := jsonb_set(
                        jsonb_set(v_policy, '{allow_open_registration}',
                            v_policy_settings->'allow_open_registration', true),
                        '{frontend_base_url}', v_policy_settings->'frontend_base_url', true
                    );
                    v_actor := aap_identity_recovery_v1._actor_record(v_actor_id);
                    v_eligible := true;
                ELSE
                    v_actor_id := NULL;
                END IF;
            END IF;
        ELSIF v_operation = 'consume_password_reset' THEN
            SELECT token.id, token.user_id INTO v_token_id, v_actor_id
            FROM public.password_reset_tokens AS token
            WHERE token.token_hash = v_token_hash;
            IF NOT FOUND THEN
                RETURN aap_identity_recovery_v1._failure('invalid_request');
            END IF;
            PERFORM 1 FROM public.users AS account
            WHERE account.id = v_actor_id FOR UPDATE;
            IF NOT FOUND THEN
                RETURN aap_identity_recovery_v1._failure('invalid_request');
            END IF;
            SELECT token.issued_email, token.expires_at, token.used_at
              INTO v_email, v_expires_at, v_used_at
            FROM public.password_reset_tokens AS token
            WHERE token.id = v_token_id AND token.user_id = v_actor_id
              AND token.token_hash = v_token_hash
            FOR UPDATE;
            SELECT account.email, account.is_active, account.role,
                   account.email_verified_at
              INTO v_user_email, v_user_active, v_user_role, v_user_verified_at
            FROM public.users AS account
            WHERE account.id = v_actor_id;
            IF NOT FOUND OR v_used_at IS NOT NULL OR v_expires_at <= pg_catalog.clock_timestamp()
               OR v_email IS DISTINCT FROM v_user_email THEN
                RETURN aap_identity_recovery_v1._failure('invalid_request');
            END IF;
            v_actor := aap_identity_recovery_v1._actor_record(v_actor_id);
            v_token := aap_identity_recovery_v1._token_record(v_operation, v_token_id);
            v_eligible := v_user_active;
        ELSE
            SELECT token.id, token.user_id INTO v_token_id, v_actor_id
            FROM public.email_verification_tokens AS token
            WHERE token.token_hash = v_token_hash;
            IF NOT FOUND THEN
                RETURN aap_identity_recovery_v1._failure('invalid_request');
            END IF;
            PERFORM 1 FROM public.users AS account
            WHERE account.id = v_actor_id FOR UPDATE;
            IF NOT FOUND THEN
                RETURN aap_identity_recovery_v1._failure('invalid_request');
            END IF;
            SELECT token.issued_email, token.expires_at, token.used_at
              INTO v_email, v_expires_at, v_used_at
            FROM public.email_verification_tokens AS token
            WHERE token.id = v_token_id AND token.user_id = v_actor_id
              AND token.token_hash = v_token_hash
            FOR UPDATE;
            SELECT account.email, account.is_active, account.role,
                   account.email_verified_at
              INTO v_user_email, v_user_active, v_user_role, v_user_verified_at
            FROM public.users AS account
            WHERE account.id = v_actor_id;
            IF NOT FOUND OR v_used_at IS NOT NULL OR v_expires_at <= pg_catalog.clock_timestamp()
               OR v_email IS DISTINCT FROM v_user_email THEN
                RETURN aap_identity_recovery_v1._failure('invalid_request');
            END IF;
            v_actor := aap_identity_recovery_v1._actor_record(v_actor_id);
            v_token := aap_identity_recovery_v1._token_record(v_operation, v_token_id);
            v_eligible := true;
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-recovery-native/v1',
        'ok', true,
        'operation', v_operation,
        'operation_id', v_operation_id,
        'data', pg_catalog.jsonb_build_object(
            'actor', v_actor,
            'password_hash', v_password_hash,
            'token', v_token,
            'eligible', v_eligible,
            'replay', v_replay,
            'policy', v_policy
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_recovery_v1.prepare_recovery_command(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_recovery_v1.apply_recovery_command(p_request jsonb)
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
REVOKE ALL ON FUNCTION aap_identity_recovery_v1.apply_recovery_command(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_recovery_v1.claim_mail(p_request jsonb)
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
    v_worker_id uuid;
    v_delivery_id uuid;
    v_lease_seconds smallint;
    v_lease_numeric numeric;
    v_candidate record;
    v_actor_email text;
    v_actor_active boolean;
    v_actor_verified_at timestamptz;
    v_token_hash text;
    v_token_email text;
    v_token_expires timestamptz;
    v_token_used_at timestamptz;
    v_outbox record;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_invalid_status text;
    v_lease_token uuid;
    v_data jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_recovery_v1._mail_failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_count <> 4 OR NOT (p_request ?& ARRAY[
        'schema', 'worker_id', 'lease_seconds', 'delivery_id'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
        WHERE field.name NOT IN ('schema', 'worker_id', 'lease_seconds', 'delivery_id')
    ) OR p_request->>'schema' IS DISTINCT FROM 'aap-identity-recovery-mail-native/v1'
       OR pg_catalog.jsonb_typeof(p_request->'worker_id') IS DISTINCT FROM 'string'
       OR (p_request->>'worker_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'lease_seconds') IS DISTINCT FROM 'number'
       OR (p_request->>'lease_seconds') !~ '^-?(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(p_request->'delivery_id') NOT IN ('null', 'string')
       OR (pg_catalog.jsonb_typeof(p_request->'delivery_id') = 'string'
           AND (p_request->>'delivery_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
        RETURN aap_identity_recovery_v1._mail_failure('invalid_request');
    END IF;
    BEGIN
        v_worker_id := (p_request->>'worker_id')::uuid;
        v_lease_numeric := (p_request->>'lease_seconds')::numeric;
        IF pg_catalog.jsonb_typeof(p_request->'delivery_id') = 'string' THEN
            v_delivery_id := (p_request->>'delivery_id')::uuid;
        END IF;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_identity_recovery_v1._mail_failure('invalid_request');
    END;
    v_lease_seconds := greatest(1, least(300, v_lease_numeric))::smallint;

    FOR v_candidate IN
        SELECT outbox.id, outbox.actor_id, outbox.challenge_id,
               outbox.purpose, outbox.token_hash
        FROM public.identity_credential_mail_outbox AS outbox
        WHERE ((outbox.status = 'pending' AND outbox.available_at <= v_now)
           OR (outbox.status = 'leased' AND outbox.lease_expires_at <= v_now))
          AND (v_delivery_id IS NULL OR outbox.id = v_delivery_id)
        ORDER BY outbox.available_at, outbox.created_at, outbox.id
        LIMIT 64
    LOOP
        SELECT account.email, account.is_active, account.email_verified_at
          INTO v_actor_email, v_actor_active, v_actor_verified_at
        FROM public.users AS account
        WHERE account.id = v_candidate.actor_id
        FOR UPDATE;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        v_invalid_status := NULL;
        v_token_hash := NULL;
        v_token_email := NULL;
        v_token_expires := NULL;
        v_token_used_at := NULL;
        IF v_candidate.purpose = 'password_reset' THEN
            SELECT token.token_hash, token.issued_email, token.expires_at, token.used_at
              INTO v_token_hash, v_token_email, v_token_expires, v_token_used_at
            FROM public.password_reset_tokens AS token
            WHERE token.id = v_candidate.challenge_id
              AND token.user_id = v_candidate.actor_id
            FOR UPDATE;
        ELSE
            SELECT token.token_hash, token.issued_email, token.expires_at, token.used_at
              INTO v_token_hash, v_token_email, v_token_expires, v_token_used_at
            FROM public.email_verification_tokens AS token
            WHERE token.id = v_candidate.challenge_id
              AND token.user_id = v_candidate.actor_id
            FOR UPDATE;
        END IF;
        IF NOT FOUND OR v_token_hash IS DISTINCT FROM v_candidate.token_hash
           OR v_token_email IS DISTINCT FROM v_actor_email
           OR v_token_used_at IS NOT NULL THEN
            v_invalid_status := 'cancelled';
        ELSIF v_token_expires <= v_now THEN
            v_invalid_status := 'expired';
        ELSIF v_candidate.purpose = 'password_reset' AND NOT v_actor_active THEN
            v_invalid_status := 'cancelled';
        ELSIF v_candidate.purpose = 'email_verification' AND v_actor_verified_at IS NOT NULL THEN
            v_invalid_status := 'cancelled';
        END IF;

        SELECT outbox.* INTO v_outbox
        FROM public.identity_credential_mail_outbox AS outbox
        WHERE outbox.id = v_candidate.id
        FOR UPDATE;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        v_now := pg_catalog.clock_timestamp();
        IF v_invalid_status IS NULL AND v_token_expires <= v_now THEN
            v_invalid_status := 'expired';
        END IF;
        IF NOT (
            (v_outbox.status = 'pending' AND v_outbox.available_at <= v_now)
            OR (v_outbox.status = 'leased' AND v_outbox.lease_expires_at <= v_now)
        ) THEN
            CONTINUE;
        END IF;
        IF v_invalid_status IS NOT NULL THEN
            UPDATE public.identity_credential_mail_outbox AS outbox
            SET status = v_invalid_status,
                worker_id = NULL, lease_token = NULL, lease_seconds = NULL,
                lease_expires_at = NULL, updated_at = v_now
            WHERE outbox.id = v_outbox.id;
            CONTINUE;
        END IF;

        v_lease_token := pg_catalog.gen_random_uuid();
        UPDATE public.identity_credential_mail_outbox AS outbox
        SET status = 'leased',
            attempt = (least(outbox.attempt::bigint + 1, 2147483647))::integer,
            worker_id = v_worker_id,
            lease_token = v_lease_token,
            lease_seconds = v_lease_seconds,
            lease_expires_at = least(
                v_now + pg_catalog.make_interval(secs => v_lease_seconds),
                outbox.expires_at
            ),
            updated_at = v_now
        WHERE outbox.id = v_outbox.id
        RETURNING pg_catalog.jsonb_build_object(
            'id', outbox.id,
            'actor_id', outbox.actor_id,
            'purpose', outbox.purpose,
            'email', outbox.email,
            'frontend_base_url', outbox.frontend_base_url,
            'token_hash', outbox.token_hash,
            'token_ciphertext', outbox.token_ciphertext,
            'encryption_id', outbox.encryption_id,
            'expires_at', outbox.expires_at,
            'attempt', outbox.attempt,
            'worker_id', outbox.worker_id,
            'lease_token', outbox.lease_token,
            'lease_expires_at', outbox.lease_expires_at
        ) INTO v_data;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-recovery-mail-native/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object('delivery', v_data)
        );
    END LOOP;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-recovery-mail-native/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object('delivery', NULL)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_recovery_v1.claim_mail(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_recovery_v1.renew_mail(p_request jsonb)
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
    v_delivery_id uuid;
    v_worker_id uuid;
    v_lease_token uuid;
    v_actor_id uuid;
    v_challenge_id uuid;
    v_purpose text;
    v_email text;
    v_token_hash text;
    v_token_email text;
    v_token_expires timestamptz;
    v_token_used_at timestamptz;
    v_actor_email text;
    v_actor_active boolean;
    v_actor_verified_at timestamptz;
    v_outbox record;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_accepted boolean := false;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_recovery_v1._mail_failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_count <> 4 OR NOT (p_request ?& ARRAY[
        'schema', 'delivery_id', 'worker_id', 'lease_token'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
        WHERE field.name NOT IN ('schema', 'delivery_id', 'worker_id', 'lease_token')
    ) OR p_request->>'schema' IS DISTINCT FROM 'aap-identity-recovery-mail-native/v1'
       OR pg_catalog.jsonb_typeof(p_request->'delivery_id') IS DISTINCT FROM 'string'
       OR (p_request->>'delivery_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'worker_id') IS DISTINCT FROM 'string'
       OR (p_request->>'worker_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'lease_token') IS DISTINCT FROM 'string'
       OR (p_request->>'lease_token') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN aap_identity_recovery_v1._mail_failure('invalid_request');
    END IF;
    BEGIN
        v_delivery_id := (p_request->>'delivery_id')::uuid;
        v_worker_id := (p_request->>'worker_id')::uuid;
        v_lease_token := (p_request->>'lease_token')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_recovery_v1._mail_failure('invalid_request');
    END;

    SELECT outbox.actor_id, outbox.challenge_id, outbox.purpose,
           outbox.email, outbox.token_hash
      INTO v_actor_id, v_challenge_id, v_purpose, v_email, v_token_hash
    FROM public.identity_credential_mail_outbox AS outbox
    WHERE outbox.id = v_delivery_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-recovery-mail-native/v1', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;
    SELECT account.email, account.is_active, account.email_verified_at
      INTO v_actor_email, v_actor_active, v_actor_verified_at
    FROM public.users AS account
    WHERE account.id = v_actor_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-recovery-mail-native/v1', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;
    IF v_purpose = 'password_reset' THEN
        SELECT token.token_hash, token.issued_email, token.expires_at, token.used_at
          INTO v_token_hash, v_token_email, v_token_expires, v_token_used_at
        FROM public.password_reset_tokens AS token
        WHERE token.id = v_challenge_id AND token.user_id = v_actor_id
        FOR UPDATE;
    ELSE
        SELECT token.token_hash, token.issued_email, token.expires_at, token.used_at
          INTO v_token_hash, v_token_email, v_token_expires, v_token_used_at
        FROM public.email_verification_tokens AS token
        WHERE token.id = v_challenge_id AND token.user_id = v_actor_id
        FOR UPDATE;
    END IF;
    SELECT outbox.* INTO v_outbox
    FROM public.identity_credential_mail_outbox AS outbox
    WHERE outbox.id = v_delivery_id
    FOR UPDATE;
    v_now := pg_catalog.clock_timestamp();
    IF NOT FOUND OR v_outbox.status <> 'leased'
       OR v_outbox.worker_id IS DISTINCT FROM v_worker_id
       OR v_outbox.lease_token IS DISTINCT FROM v_lease_token
       OR v_outbox.lease_expires_at <= v_now
       OR v_outbox.expires_at <= v_now
       OR v_token_hash IS DISTINCT FROM v_outbox.token_hash
       OR v_token_email IS DISTINCT FROM v_actor_email
       OR v_token_used_at IS NOT NULL
       OR v_outbox.email IS DISTINCT FROM v_actor_email
       OR (v_purpose = 'password_reset' AND NOT v_actor_active)
       OR (v_purpose = 'email_verification' AND v_actor_verified_at IS NOT NULL) THEN
        v_accepted := false;
    ELSE
        UPDATE public.identity_credential_mail_outbox AS outbox
        SET lease_expires_at = least(
                v_now + pg_catalog.make_interval(secs => v_outbox.lease_seconds),
                outbox.expires_at
            ),
            updated_at = v_now
        WHERE outbox.id = v_delivery_id;
        v_accepted := true;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-recovery-mail-native/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object('accepted', v_accepted)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_recovery_v1.renew_mail(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_recovery_v1.finish_mail(p_request jsonb)
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
    v_delivery_id uuid;
    v_worker_id uuid;
    v_lease_token uuid;
    v_outcome text;
    v_reason text;
    v_retry_numeric numeric;
    v_retry_seconds integer := 30;
    v_actor_id uuid;
    v_challenge_id uuid;
    v_purpose text;
    v_token_hash text;
    v_token_email text;
    v_token_expires timestamptz;
    v_token_used_at timestamptz;
    v_actor_email text;
    v_actor_active boolean;
    v_actor_verified_at timestamptz;
    v_outbox record;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_accepted boolean := false;
    v_terminal_status text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_recovery_v1._mail_failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_count <> 7 OR NOT (p_request ?& ARRAY[
        'schema', 'delivery_id', 'worker_id', 'lease_token', 'outcome',
        'reason', 'retry_delay_seconds'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
        WHERE field.name NOT IN (
            'schema', 'delivery_id', 'worker_id', 'lease_token', 'outcome',
            'reason', 'retry_delay_seconds'
        )
    ) OR p_request->>'schema' IS DISTINCT FROM 'aap-identity-recovery-mail-native/v1'
       OR pg_catalog.jsonb_typeof(p_request->'delivery_id') IS DISTINCT FROM 'string'
       OR (p_request->>'delivery_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'worker_id') IS DISTINCT FROM 'string'
       OR (p_request->>'worker_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'lease_token') IS DISTINCT FROM 'string'
       OR (p_request->>'lease_token') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'outcome') IS DISTINCT FROM 'string'
       OR p_request->>'outcome' NOT IN ('delivered', 'rejected', 'retry', 'unknown', 'invalid_key')
       OR pg_catalog.jsonb_typeof(p_request->'reason') IS DISTINCT FROM 'string'
       OR p_request->>'reason' NOT IN (
           'sent', 'not_configured', 'message_invalid', 'tls_failed', 'auth_failed',
           'recipient_rejected', 'sender_rejected', 'data_rejected', 'connect_failed',
           'transport_failed', 'derivation_unavailable'
       )
       OR pg_catalog.jsonb_typeof(p_request->'retry_delay_seconds') NOT IN ('null', 'number')
       OR (pg_catalog.jsonb_typeof(p_request->'retry_delay_seconds') = 'number'
           AND (p_request->>'retry_delay_seconds') !~ '^-?(0|[1-9][0-9]*)$') THEN
        RETURN aap_identity_recovery_v1._mail_failure('invalid_request');
    END IF;
    BEGIN
        v_delivery_id := (p_request->>'delivery_id')::uuid;
        v_worker_id := (p_request->>'worker_id')::uuid;
        v_lease_token := (p_request->>'lease_token')::uuid;
        IF pg_catalog.jsonb_typeof(p_request->'retry_delay_seconds') = 'number' THEN
            v_retry_numeric := (p_request->>'retry_delay_seconds')::numeric;
        END IF;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_identity_recovery_v1._mail_failure('invalid_request');
    END;
    v_outcome := p_request->>'outcome';
    v_reason := p_request->>'reason';
    IF (v_outcome = 'delivered' AND v_reason <> 'sent')
       OR (v_outcome = 'invalid_key' AND v_reason <> 'derivation_unavailable')
       OR (v_outcome IN ('delivered', 'rejected')
           AND pg_catalog.jsonb_typeof(p_request->'retry_delay_seconds') <> 'null') THEN
        RETURN aap_identity_recovery_v1._mail_failure('invalid_request');
    END IF;
    IF v_outcome IN ('retry', 'unknown', 'invalid_key') THEN
        v_retry_seconds := greatest(1, least(300, COALESCE(v_retry_numeric, 30)))::integer;
    END IF;

    SELECT outbox.actor_id, outbox.challenge_id, outbox.purpose,
           outbox.token_hash
      INTO v_actor_id, v_challenge_id, v_purpose, v_token_hash
    FROM public.identity_credential_mail_outbox AS outbox
    WHERE outbox.id = v_delivery_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-recovery-mail-native/v1', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;
    SELECT account.email, account.is_active, account.email_verified_at
      INTO v_actor_email, v_actor_active, v_actor_verified_at
    FROM public.users AS account
    WHERE account.id = v_actor_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-recovery-mail-native/v1', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;
    IF v_purpose = 'password_reset' THEN
        SELECT token.token_hash, token.issued_email, token.expires_at, token.used_at
          INTO v_token_hash, v_token_email, v_token_expires, v_token_used_at
        FROM public.password_reset_tokens AS token
        WHERE token.id = v_challenge_id AND token.user_id = v_actor_id
        FOR UPDATE;
    ELSE
        SELECT token.token_hash, token.issued_email, token.expires_at, token.used_at
          INTO v_token_hash, v_token_email, v_token_expires, v_token_used_at
        FROM public.email_verification_tokens AS token
        WHERE token.id = v_challenge_id AND token.user_id = v_actor_id
        FOR UPDATE;
    END IF;
    SELECT outbox.* INTO v_outbox
    FROM public.identity_credential_mail_outbox AS outbox
    WHERE outbox.id = v_delivery_id
    FOR UPDATE;
    v_now := pg_catalog.clock_timestamp();
    IF NOT FOUND OR v_outbox.status <> 'leased'
       OR v_outbox.worker_id IS DISTINCT FROM v_worker_id
       OR v_outbox.lease_token IS DISTINCT FROM v_lease_token
       OR v_outbox.lease_expires_at <= v_now THEN
        -- A late SMTP completion must not overwrite a lease held by another worker.
        v_accepted := false;
    ELSIF v_token_hash IS DISTINCT FROM v_outbox.token_hash
       OR v_token_email IS DISTINCT FROM v_actor_email
       OR v_token_used_at IS NOT NULL
       OR v_outbox.email IS DISTINCT FROM v_actor_email
       OR (v_purpose = 'password_reset' AND NOT v_actor_active)
       OR (v_purpose = 'email_verification' AND v_actor_verified_at IS NOT NULL) THEN
        v_terminal_status := 'cancelled';
        UPDATE public.identity_credential_mail_outbox AS outbox
        SET status = v_terminal_status, worker_id = NULL, lease_token = NULL,
            lease_seconds = NULL, lease_expires_at = NULL, last_reason = v_reason,
            updated_at = v_now
        WHERE outbox.id = v_delivery_id;
        v_accepted := true;
    ELSIF v_outbox.expires_at <= v_now OR v_token_expires <= v_now THEN
        UPDATE public.identity_credential_mail_outbox AS outbox
        SET status = 'expired', worker_id = NULL, lease_token = NULL,
            lease_seconds = NULL, lease_expires_at = NULL, last_reason = v_reason,
            updated_at = v_now
        WHERE outbox.id = v_delivery_id;
        v_accepted := true;
    ELSE
        IF v_outcome = 'rejected' AND v_purpose = 'password_reset' THEN
            UPDATE public.password_reset_tokens AS token
            SET used_at = COALESCE(token.used_at, v_now)
            WHERE token.id = v_challenge_id AND token.user_id = v_actor_id
              AND token.token_hash = v_outbox.token_hash;
        END IF;
        IF v_outcome = 'delivered' THEN
            v_terminal_status := 'delivered';
        ELSIF v_outcome = 'rejected' THEN
            v_terminal_status := 'rejected';
        ELSE
            v_terminal_status := 'pending';
        END IF;
        UPDATE public.identity_credential_mail_outbox AS outbox
        SET status = v_terminal_status,
            available_at = CASE WHEN v_terminal_status = 'pending'
                THEN least(v_now + pg_catalog.make_interval(secs => v_retry_seconds), outbox.expires_at)
                ELSE outbox.available_at END,
            worker_id = NULL, lease_token = NULL, lease_seconds = NULL,
            lease_expires_at = NULL, last_reason = v_reason, updated_at = v_now
        WHERE outbox.id = v_delivery_id;
        v_accepted := true;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-recovery-mail-native/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object('accepted', v_accepted)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_recovery_v1.finish_mail(jsonb) FROM PUBLIC;
