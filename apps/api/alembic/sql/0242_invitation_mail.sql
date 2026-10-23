-- Versioned invitation mail shares the encrypted credential-mail queue.
-- Existing v1 recovery signatures are replaced below with recovery-only guards.
ALTER TABLE public.identity_credential_mail_outbox
    DROP CONSTRAINT IF EXISTS identity_credential_mail_outbox_purpose_check;
-- aap-statement-break
ALTER TABLE public.identity_credential_mail_outbox
    ALTER COLUMN challenge_id DROP NOT NULL;
-- aap-statement-break
ALTER TABLE public.identity_credential_mail_outbox
    ADD COLUMN invitation_id uuid;
-- aap-statement-break
ALTER TABLE public.identity_credential_mail_outbox
    ADD COLUMN invitation_context jsonb;
-- aap-statement-break
ALTER TABLE public.identity_credential_mail_outbox
    ADD CONSTRAINT fk_identity_credential_mail_invitation
        FOREIGN KEY (invitation_id)
        REFERENCES public.user_invitations(id)
        ON DELETE CASCADE;
-- aap-statement-break
ALTER TABLE public.identity_credential_mail_outbox
    ADD CONSTRAINT ck_identity_credential_mail_purpose_v2
        CHECK (purpose IN ('password_reset', 'email_verification', 'invitation'));
-- aap-statement-break
ALTER TABLE public.identity_credential_mail_outbox
    ADD CONSTRAINT ck_identity_credential_mail_purpose_shape_v2 CHECK (
        (
            purpose IN ('password_reset', 'email_verification')
            AND challenge_id IS NOT NULL
            AND invitation_id IS NULL
            AND invitation_context IS NULL
        )
        OR (
            purpose = 'invitation'
            AND challenge_id IS NULL
            AND invitation_id IS NOT NULL
            AND invitation_context IS NOT NULL
        )
    );
-- aap-statement-break
ALTER TABLE public.identity_credential_mail_outbox
    ADD CONSTRAINT ck_identity_credential_mail_invitation_context_v2 CHECK (
        purpose <> 'invitation'
        OR (
            pg_catalog.jsonb_typeof(invitation_context) = 'object'
            AND invitation_context ?& ARRAY[
                'project_name', 'project_member_role', 'sender_actor', 'audit_context'
            ]
            AND invitation_context - ARRAY[
                'project_name', 'project_member_role', 'sender_actor', 'audit_context'
            ] = '{}'::jsonb
            AND pg_catalog.jsonb_typeof(invitation_context->'project_name')
                IN ('string', 'null')
            AND (
                pg_catalog.jsonb_typeof(invitation_context->'project_name') = 'null'
                OR (
                    pg_catalog.char_length(invitation_context->>'project_name') <= 255
                )
            )
            AND pg_catalog.jsonb_typeof(invitation_context->'project_member_role')
                IN ('string', 'null')
            AND (
                pg_catalog.jsonb_typeof(invitation_context->'project_member_role') = 'null'
                OR invitation_context->>'project_member_role'
                    IN ('annotator', 'reviewer', 'viewer')
            )
            AND pg_catalog.jsonb_typeof(invitation_context->'sender_actor') = 'object'
            AND (invitation_context->'sender_actor') ?& ARRAY['actor_id', 'email', 'role']
            AND (invitation_context->'sender_actor') - ARRAY['actor_id', 'email', 'role'] = '{}'::jsonb
            AND pg_catalog.jsonb_typeof(invitation_context->'sender_actor'->'actor_id') = 'string'
            AND (invitation_context->'sender_actor'->>'actor_id')
                ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            AND pg_catalog.jsonb_typeof(invitation_context->'sender_actor'->'email') = 'string'
            AND pg_catalog.char_length(invitation_context->'sender_actor'->>'email') BETWEEN 1 AND 255
            AND pg_catalog.jsonb_typeof(invitation_context->'sender_actor'->'role') = 'string'
            AND invitation_context->'sender_actor'->>'role' IN ('super_admin', 'project_admin')
            AND pg_catalog.jsonb_typeof(invitation_context->'audit_context') = 'object'
            AND (invitation_context->'audit_context') ?& ARRAY[
                'method', 'path', 'ip', 'request_id'
            ]
            AND (invitation_context->'audit_context') - ARRAY[
                'method', 'path', 'ip', 'request_id'
            ] = '{}'::jsonb
            AND pg_catalog.jsonb_typeof(invitation_context->'audit_context'->'method')
                IN ('string', 'null')
            AND pg_catalog.jsonb_typeof(invitation_context->'audit_context'->'path')
                IN ('string', 'null')
            AND pg_catalog.jsonb_typeof(invitation_context->'audit_context'->'ip')
                IN ('string', 'null')
            AND pg_catalog.jsonb_typeof(invitation_context->'audit_context'->'request_id')
                IN ('string', 'null')
        )
    );
-- aap-statement-break
CREATE INDEX ix_identity_credential_mail_invitation
    ON public.identity_credential_mail_outbox (invitation_id, created_at DESC)
    WHERE purpose = 'invitation';
-- aap-statement-break
COMMENT ON COLUMN public.identity_credential_mail_outbox.invitation_context IS
    'Frozen message and Audit origin for invitation delivery; contains no raw proof token or link.';
-- aap-statement-break
CREATE SCHEMA IF NOT EXISTS aap_identity_mail_v2;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_mail_v2 FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_identity_recovery_v1.claim_mail(p_request jsonb)
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
        WHERE outbox.purpose IN ('password_reset', 'email_verification')
          AND ((outbox.status = 'pending' AND outbox.available_at <= v_now)
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
CREATE OR REPLACE FUNCTION aap_identity_recovery_v1.renew_mail(p_request jsonb)
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
    IF v_purpose NOT IN ('password_reset', 'email_verification') THEN
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
CREATE OR REPLACE FUNCTION aap_identity_recovery_v1.finish_mail(p_request jsonb)
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
    IF v_purpose NOT IN ('password_reset', 'email_verification') THEN
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
-- aap-statement-break
CREATE FUNCTION aap_identity_mail_v2.mail_policy()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-mail-policy/v2',
        'policy', 'aap-identity-mail/v2'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_mail_v2.mail_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_mail_v2.claim_mail(p_request jsonb)
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
    v_scope jsonb;
    v_scope_data jsonb;
    v_scope_code text;
    v_outbox record;
    v_legacy_request jsonb;
    v_legacy_reply jsonb;
    v_now timestamptz;
    v_lease_token uuid;
    v_delivery jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_count <> 4 OR NOT (p_request ?& ARRAY[
        'schema', 'worker_id', 'lease_seconds', 'delivery_id'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
        WHERE field.name NOT IN ('schema', 'worker_id', 'lease_seconds', 'delivery_id')
    ) OR p_request->>'schema' IS DISTINCT FROM 'aap-identity-mail-native/v2'
       OR pg_catalog.jsonb_typeof(p_request->'worker_id') IS DISTINCT FROM 'string'
       OR (p_request->>'worker_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'lease_seconds') IS DISTINCT FROM 'number'
       OR (p_request->>'lease_seconds') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(p_request->'delivery_id') NOT IN ('null', 'string')
       OR (pg_catalog.jsonb_typeof(p_request->'delivery_id') = 'string'
           AND (p_request->>'delivery_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    BEGIN
        v_worker_id := (p_request->>'worker_id')::uuid;
        v_lease_numeric := (p_request->>'lease_seconds')::numeric;
        IF pg_catalog.jsonb_typeof(p_request->'delivery_id') = 'string' THEN
            v_delivery_id := (p_request->>'delivery_id')::uuid;
        END IF;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END;
    IF v_lease_numeric NOT BETWEEN 1 AND 300 THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    v_lease_seconds := v_lease_numeric::smallint;

    FOR v_candidate IN
        SELECT outbox.id, outbox.purpose, outbox.invitation_id, outbox.token_hash
        FROM public.identity_credential_mail_outbox AS outbox
        WHERE ((outbox.status = 'pending' AND outbox.available_at <= pg_catalog.clock_timestamp())
           OR (outbox.status = 'leased' AND outbox.lease_expires_at <= pg_catalog.clock_timestamp()))
          AND (v_delivery_id IS NULL OR outbox.id = v_delivery_id)
        ORDER BY outbox.available_at, outbox.created_at, outbox.id
        LIMIT 64
    LOOP
        IF v_candidate.purpose IN ('password_reset', 'email_verification') THEN
            v_legacy_request := pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-recovery-mail-native/v1',
                'worker_id', v_worker_id,
                'lease_seconds', v_lease_seconds,
                'delivery_id', v_candidate.id
            );
            v_legacy_reply := aap_identity_recovery_v1.claim_mail(v_legacy_request);
            IF pg_catalog.jsonb_typeof(v_legacy_reply) IS DISTINCT FROM 'object'
               OR v_legacy_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                IF v_legacy_reply->'ok' IS DISTINCT FROM 'false'::jsonb THEN
                    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Recovery mail reply';
                END IF;
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-identity-mail-native/v2', 'ok', false,
                    'error', v_legacy_reply->'error'
                );
            END IF;
            v_delivery := v_legacy_reply->'data'->'delivery';
            IF v_delivery IS NOT NULL AND v_delivery <> 'null'::jsonb THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-identity-mail-native/v2', 'ok', true,
                    'data', pg_catalog.jsonb_build_object('delivery', v_delivery)
                );
            END IF;
            CONTINUE;
        END IF;
        IF v_candidate.purpose IS DISTINCT FROM 'invitation'
           OR v_candidate.invitation_id IS NULL THEN
            CONTINUE;
        END IF;

        v_scope := aap_identity_invitations_v1.lock_invitation_mail_scope(
            v_candidate.invitation_id, v_candidate.token_hash
        );
        IF pg_catalog.jsonb_typeof(v_scope) IS DISTINCT FROM 'object'
           OR v_scope->>'schema' IS DISTINCT FROM 'aap-identity-invitation-mail-scope/v1' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation mail scope reply';
        END IF;
        IF v_scope->'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_scope_code := v_scope->'error'->>'code';
            IF v_scope->'ok' IS DISTINCT FROM 'false'::jsonb
               OR v_scope_code IS NULL
               OR v_scope_code NOT IN ('invitation_superseded', 'invitation_not_sendable') THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation mail scope reply';
            END IF;
            SELECT outbox.* INTO v_outbox
            FROM public.identity_credential_mail_outbox AS outbox
            WHERE outbox.id = v_candidate.id
            FOR UPDATE NOWAIT;
            IF FOUND THEN
                v_now := pg_catalog.clock_timestamp();
                UPDATE public.identity_credential_mail_outbox AS outbox
                SET status = CASE WHEN outbox.expires_at <= v_now THEN 'expired' ELSE 'cancelled' END,
                    worker_id = NULL, lease_token = NULL, lease_seconds = NULL,
                    lease_expires_at = NULL, updated_at = v_now
                WHERE outbox.id = v_outbox.id
                  AND outbox.purpose = 'invitation'
                  AND ((outbox.status = 'pending' AND outbox.available_at <= v_now)
                       OR (outbox.status = 'leased' AND outbox.lease_expires_at <= v_now));
            END IF;
            CONTINUE;
        END IF;
        v_scope_data := v_scope->'data';
        IF pg_catalog.jsonb_typeof(v_scope_data) IS DISTINCT FROM 'object'
           OR v_scope_data->>'invitation_id' IS DISTINCT FROM v_candidate.invitation_id::text
           OR v_scope_data->>'token_hash' IS DISTINCT FROM v_candidate.token_hash THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation mail scope binding';
        END IF;

        SELECT outbox.* INTO v_outbox
        FROM public.identity_credential_mail_outbox AS outbox
        WHERE outbox.id = v_candidate.id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        v_now := pg_catalog.clock_timestamp();
        IF v_outbox.purpose IS DISTINCT FROM 'invitation'
           OR v_outbox.invitation_id IS DISTINCT FROM v_candidate.invitation_id
           OR v_outbox.actor_id IS DISTINCT FROM (v_scope_data->>'invited_by')::uuid
           OR v_outbox.token_hash IS DISTINCT FROM (v_scope_data->>'token_hash')
           OR v_outbox.email IS DISTINCT FROM (v_scope_data->>'email')
           OR v_outbox.expires_at IS DISTINCT FROM (v_scope_data->>'expires_at')::timestamptz
           OR NOT (
               (v_outbox.status = 'pending' AND v_outbox.available_at <= v_now)
               OR (v_outbox.status = 'leased' AND v_outbox.lease_expires_at <= v_now)
           ) THEN
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
            'issuer_id', outbox.actor_id,
            'invitation_id', outbox.invitation_id,
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
            'lease_expires_at', outbox.lease_expires_at,
            'project_name', outbox.invitation_context->'project_name',
            'project_member_role', outbox.invitation_context->'project_member_role'
        ) INTO v_delivery;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', true,
            'data', pg_catalog.jsonb_build_object('delivery', v_delivery)
        );
    END LOOP;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-mail-native/v2', 'ok', true,
        'data', pg_catalog.jsonb_build_object('delivery', NULL)
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_mail_v2.claim_mail(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_mail_v2.renew_mail(p_request jsonb)
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
    v_purpose text;
    v_invitation_id uuid;
    v_token_hash text;
    v_scope jsonb;
    v_scope_data jsonb;
    v_legacy_request jsonb;
    v_legacy_reply jsonb;
    v_outbox record;
    v_now timestamptz;
    v_accepted boolean := false;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_count <> 4 OR NOT (p_request ?& ARRAY[
        'schema', 'delivery_id', 'worker_id', 'lease_token'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
        WHERE field.name NOT IN ('schema', 'delivery_id', 'worker_id', 'lease_token')
    ) OR p_request->>'schema' IS DISTINCT FROM 'aap-identity-mail-native/v2'
       OR pg_catalog.jsonb_typeof(p_request->'delivery_id') IS DISTINCT FROM 'string'
       OR (p_request->>'delivery_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'worker_id') IS DISTINCT FROM 'string'
       OR (p_request->>'worker_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'lease_token') IS DISTINCT FROM 'string'
       OR (p_request->>'lease_token') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    BEGIN
        v_delivery_id := (p_request->>'delivery_id')::uuid;
        v_worker_id := (p_request->>'worker_id')::uuid;
        v_lease_token := (p_request->>'lease_token')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END;

    SELECT outbox.purpose, outbox.invitation_id, outbox.token_hash
      INTO v_purpose, v_invitation_id, v_token_hash
    FROM public.identity_credential_mail_outbox AS outbox
    WHERE outbox.id = v_delivery_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;
    IF v_purpose IN ('password_reset', 'email_verification') THEN
        v_legacy_request := pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-recovery-mail-native/v1',
            'delivery_id', v_delivery_id,
            'worker_id', v_worker_id,
            'lease_token', v_lease_token
        );
        v_legacy_reply := aap_identity_recovery_v1.renew_mail(v_legacy_request);
        IF v_legacy_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-mail-native/v2', 'ok', false,
                'error', v_legacy_reply->'error'
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', true,
            'data', v_legacy_reply->'data'
        );
    END IF;
    IF v_purpose IS DISTINCT FROM 'invitation' OR v_invitation_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;

    v_scope := aap_identity_invitations_v1.lock_invitation_mail_scope(
        v_invitation_id, v_token_hash
    );
    IF pg_catalog.jsonb_typeof(v_scope) IS DISTINCT FROM 'object'
       OR v_scope->>'schema' IS DISTINCT FROM 'aap-identity-invitation-mail-scope/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation mail scope reply';
    END IF;
    IF v_scope->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        -- A stale scope is mutation-free; the client can roll this call back.
        IF v_scope->'ok' IS DISTINCT FROM 'false'::jsonb
           OR v_scope->'error'->>'code' IS NULL
           OR v_scope->'error'->>'code' NOT IN (
               'invitation_superseded', 'invitation_not_sendable'
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation mail scope reply';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;
    v_scope_data := v_scope->'data';
    IF pg_catalog.jsonb_typeof(v_scope_data) IS DISTINCT FROM 'object'
       OR v_scope_data->>'invitation_id' IS DISTINCT FROM v_invitation_id::text
       OR v_scope_data->>'token_hash' IS DISTINCT FROM v_token_hash THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation mail scope binding';
    END IF;

    SELECT outbox.* INTO v_outbox
    FROM public.identity_credential_mail_outbox AS outbox
    WHERE outbox.id = v_delivery_id
    FOR UPDATE NOWAIT;
    v_now := pg_catalog.clock_timestamp();
    IF FOUND AND v_outbox.status = 'leased'
       AND v_outbox.worker_id = v_worker_id
       AND v_outbox.lease_token = v_lease_token
       AND v_outbox.lease_expires_at > v_now
       AND v_outbox.expires_at > v_now
       AND v_outbox.email = v_scope_data->>'email'
       AND v_outbox.token_hash = v_scope_data->>'token_hash'
       AND v_outbox.actor_id = (v_scope_data->>'invited_by')::uuid
       AND v_outbox.expires_at = (v_scope_data->>'expires_at')::timestamptz THEN
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
        'schema', 'aap-identity-mail-native/v2', 'ok', true,
        'data', pg_catalog.jsonb_build_object('accepted', v_accepted)
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_mail_v2.renew_mail(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_mail_v2.finish_mail(p_request jsonb)
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
    v_purpose text;
    v_invitation_id uuid;
    v_token_hash text;
    v_scope jsonb;
    v_scope_data jsonb;
    v_legacy_request jsonb;
    v_legacy_reply jsonb;
    v_outbox record;
    v_now timestamptz;
    v_accepted boolean := false;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
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
    ) OR p_request->>'schema' IS DISTINCT FROM 'aap-identity-mail-native/v2'
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
           AND (p_request->>'retry_delay_seconds') !~ '^(0|[1-9][0-9]*)$') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    BEGIN
        v_delivery_id := (p_request->>'delivery_id')::uuid;
        v_worker_id := (p_request->>'worker_id')::uuid;
        v_lease_token := (p_request->>'lease_token')::uuid;
        IF pg_catalog.jsonb_typeof(p_request->'retry_delay_seconds') = 'number' THEN
            v_retry_numeric := (p_request->>'retry_delay_seconds')::numeric;
        END IF;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END;
    v_outcome := p_request->>'outcome';
    v_reason := p_request->>'reason';
    IF (v_outcome = 'delivered' AND v_reason <> 'sent')
       OR (v_outcome = 'invalid_key' AND v_reason <> 'derivation_unavailable')
       OR (v_outcome <> 'delivered' AND v_reason = 'sent')
       OR (v_outcome <> 'invalid_key' AND v_reason = 'derivation_unavailable')
       OR (v_outcome IN ('delivered', 'rejected')
           AND pg_catalog.jsonb_typeof(p_request->'retry_delay_seconds') <> 'null')
       OR (v_outcome IN ('retry', 'unknown', 'invalid_key')
           AND pg_catalog.jsonb_typeof(p_request->'retry_delay_seconds') <> 'number') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    IF v_outcome IN ('retry', 'unknown', 'invalid_key') THEN
        v_retry_seconds := greatest(1, least(300, COALESCE(v_retry_numeric, 30)))::integer;
    END IF;

    SELECT outbox.purpose, outbox.invitation_id, outbox.token_hash
      INTO v_purpose, v_invitation_id, v_token_hash
    FROM public.identity_credential_mail_outbox AS outbox
    WHERE outbox.id = v_delivery_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;
    IF v_purpose IN ('password_reset', 'email_verification') THEN
        v_legacy_request := pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-recovery-mail-native/v1',
            'delivery_id', v_delivery_id,
            'worker_id', v_worker_id,
            'lease_token', v_lease_token,
            'outcome', v_outcome,
            'reason', v_reason,
            'retry_delay_seconds', CASE
                WHEN v_outcome IN ('retry', 'unknown', 'invalid_key') THEN v_retry_seconds
                ELSE NULL END
        );
        v_legacy_reply := aap_identity_recovery_v1.finish_mail(v_legacy_request);
        IF v_legacy_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-mail-native/v2', 'ok', false,
                'error', v_legacy_reply->'error'
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', true,
            'data', v_legacy_reply->'data'
        );
    END IF;
    IF v_purpose IS DISTINCT FROM 'invitation' OR v_invitation_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;

    v_scope := aap_identity_invitations_v1.lock_invitation_mail_scope(
        v_invitation_id, v_token_hash
    );
    IF pg_catalog.jsonb_typeof(v_scope) IS DISTINCT FROM 'object'
       OR v_scope->>'schema' IS DISTINCT FROM 'aap-identity-invitation-mail-scope/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation mail scope reply';
    END IF;
    IF v_scope->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        -- A stale scope is mutation-free; the client can roll this call back.
        IF v_scope->'ok' IS DISTINCT FROM 'false'::jsonb
           OR v_scope->'error'->>'code' IS NULL
           OR v_scope->'error'->>'code' NOT IN (
               'invitation_superseded', 'invitation_not_sendable'
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation mail scope reply';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;
    v_scope_data := v_scope->'data';
    IF pg_catalog.jsonb_typeof(v_scope_data) IS DISTINCT FROM 'object'
       OR v_scope_data->>'invitation_id' IS DISTINCT FROM v_invitation_id::text
       OR v_scope_data->>'token_hash' IS DISTINCT FROM v_token_hash THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation mail scope binding';
    END IF;

    SELECT outbox.* INTO v_outbox
    FROM public.identity_credential_mail_outbox AS outbox
    WHERE outbox.id = v_delivery_id
    FOR UPDATE NOWAIT;
    v_now := pg_catalog.clock_timestamp();
    IF NOT FOUND OR v_outbox.status <> 'leased'
       OR v_outbox.worker_id IS DISTINCT FROM v_worker_id
       OR v_outbox.lease_token IS DISTINCT FROM v_lease_token
       OR v_outbox.lease_expires_at <= v_now
       OR v_outbox.expires_at <= v_now
       OR v_outbox.email IS DISTINCT FROM (v_scope_data->>'email')
       OR v_outbox.token_hash IS DISTINCT FROM (v_scope_data->>'token_hash')
       OR v_outbox.actor_id IS DISTINCT FROM (v_scope_data->>'invited_by')::uuid
       OR v_outbox.expires_at IS DISTINCT FROM (v_scope_data->>'expires_at')::timestamptz THEN
        -- Stale leases are mutation-free; only the claim path retires stale intents.
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-mail-native/v2', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;

    IF v_outcome = 'delivered' THEN
        UPDATE public.identity_credential_mail_outbox AS outbox
        SET status = 'delivered',
            worker_id = NULL, lease_token = NULL, lease_seconds = NULL,
            lease_expires_at = NULL, last_reason = v_reason, updated_at = v_now
        WHERE outbox.id = v_delivery_id;
        PERFORM aap_audit_v1.record_identity_invitation_event(
            'invitation.send_email',
            v_outbox.invitation_context->'sender_actor',
            pg_catalog.jsonb_build_object(
                'type', 'invitation', 'id', v_invitation_id::text
            ),
            v_outbox.invitation_context->'audit_context',
            pg_catalog.jsonb_build_object(
                'email', v_outbox.email,
                'project_id', v_scope_data->'project_id'
            )
        );
    ELSIF v_outcome = 'rejected' THEN
        UPDATE public.identity_credential_mail_outbox AS outbox
        SET status = 'rejected',
            worker_id = NULL, lease_token = NULL, lease_seconds = NULL,
            lease_expires_at = NULL, last_reason = v_reason, updated_at = v_now
        WHERE outbox.id = v_delivery_id;
    ELSE
        UPDATE public.identity_credential_mail_outbox AS outbox
        SET status = 'pending',
            available_at = least(
                v_now + pg_catalog.make_interval(secs => v_retry_seconds),
                outbox.expires_at
            ),
            worker_id = NULL, lease_token = NULL, lease_seconds = NULL,
            lease_expires_at = NULL, last_reason = v_reason, updated_at = v_now
        WHERE outbox.id = v_delivery_id;
    END IF;
    v_accepted := true;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-mail-native/v2', 'ok', true,
        'data', pg_catalog.jsonb_build_object('accepted', v_accepted)
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_mail_v2.finish_mail(jsonb) FROM PUBLIC;
