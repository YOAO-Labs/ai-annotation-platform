-- Durable Identity admission and session revocation authority.
-- Callers retain transaction ownership; no function commits.
ALTER TABLE public.users
    ADD COLUMN credential_generation bigint NOT NULL DEFAULT 0
    CHECK (credential_generation >= 0);
-- aap-statement-break
CREATE TABLE public.identity_revoked_jtis (
    jti uuid PRIMARY KEY,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE INDEX ix_identity_revoked_jtis_expires_at
    ON public.identity_revoked_jtis (expires_at, jti);
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_revoked_jtis FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1._admission_failure(p_code text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1._admission_failure(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.authentication_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-policy/v1',
        'issuer', 'aap-identity/v1',
        'refresh_grace_seconds', 604800
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.authentication_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.read_jwt_principal(
    p_actor_id uuid,
    p_jti uuid,
    p_generation bigint,
    p_expires_at timestamptz,
    p_for_refresh boolean
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
    v_name text;
    v_email text;
    v_role text;
    v_is_active boolean;
    v_generation bigint;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_actor_id IS NULL OR p_jti IS NULL OR p_generation IS NULL
       OR p_generation < 0 OR p_expires_at IS NULL
       OR NOT pg_catalog.isfinite(p_expires_at) OR p_for_refresh IS NULL THEN
        RETURN aap_identity_v1._admission_failure('invalid_request');
    END IF;
    IF aap_identity_v1.is_jti_revoked(p_jti) THEN
        RETURN aap_identity_v1._admission_failure('credential_revoked');
    END IF;

    SELECT actor.name, actor.email, actor.role, actor.is_active,
           actor.credential_generation
    INTO v_name, v_email, v_role, v_is_active, v_generation
    FROM public.users AS actor
    WHERE actor.id = p_actor_id;
    IF NOT FOUND THEN
        RETURN aap_identity_v1._admission_failure('credential_invalid');
    END IF;
    IF NOT v_is_active THEN
        RETURN aap_identity_v1._admission_failure('user_inactive');
    END IF;
    IF v_role IS NULL OR v_role NOT IN (
        'super_admin', 'project_admin', 'employee', 'viewer'
    ) THEN
        RETURN aap_identity_v1._admission_failure('invalid_account_role');
    END IF;
    IF v_generation IS DISTINCT FROM p_generation THEN
        RETURN aap_identity_v1._admission_failure('generation_outdated');
    END IF;
    IF (NOT p_for_refresh AND p_expires_at <= v_now)
       OR (p_for_refresh AND p_expires_at <=
           v_now - INTERVAL '604800 seconds') THEN
        RETURN aap_identity_v1._admission_failure('token_expired');
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'name', v_name,
            'email', v_email,
            'role', v_role,
            'is_active', true,
            'credential_type', 'jwt',
            'credential_id', p_jti,
            'generation', v_generation,
            'scopes', NULL,
            'expires_at', p_expires_at
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.read_jwt_principal(
    uuid, uuid, bigint, timestamptz, boolean
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.read_api_key_candidates(
    p_prefix text,
    p_after_id uuid,
    p_limit integer
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_items jsonb;
    v_count integer;
    v_last_id uuid;
    v_next_after uuid;
BEGIN
    IF p_prefix IS NULL OR pg_catalog.octet_length(p_prefix) <> 12
       OR p_prefix COLLATE "C" !~ '^ak_[A-Za-z0-9_-]{9}$'
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
        RETURN aap_identity_v1._admission_failure('invalid_request');
    END IF;

    WITH candidates AS (
        SELECT api_key.id, api_key.key_hash
        FROM public.api_keys AS api_key
        JOIN public.users AS actor ON actor.id = api_key.user_id
        WHERE api_key.key_prefix = p_prefix
          AND api_key.revoked_at IS NULL
          AND actor.is_active
          AND (p_after_id IS NULL OR api_key.id > p_after_id)
        ORDER BY api_key.id
        LIMIT p_limit + 1
    ), page AS (
        SELECT candidate.id, candidate.key_hash
        FROM candidates AS candidate
        ORDER BY candidate.id
        LIMIT p_limit
    )
    SELECT COALESCE(
               pg_catalog.jsonb_agg(
                   pg_catalog.jsonb_build_object(
                       'id', page.id, 'key_hash', page.key_hash
                   ) ORDER BY page.id
               ),
               '[]'::jsonb
           ),
           pg_catalog.count(*)::integer,
           pg_catalog.max(page.id)
    INTO v_items, v_count, v_last_id
    FROM page;

    IF v_count = p_limit AND EXISTS (
        SELECT 1
        FROM public.api_keys AS api_key
        JOIN public.users AS actor ON actor.id = api_key.user_id
        WHERE api_key.key_prefix = p_prefix
          AND api_key.revoked_at IS NULL
          AND actor.is_active
          AND api_key.id > v_last_id
    ) THEN
        v_next_after := v_last_id;
    ELSE
        v_next_after := NULL;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-candidates/v1',
        'items', v_items,
        'next_after', v_next_after
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.read_api_key_candidates(text, uuid, integer)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.confirm_api_key_principal(
    p_key_id uuid,
    p_expected_hash text
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
    v_actor_id uuid;
    v_user_id uuid;
    v_name text;
    v_email text;
    v_role text;
    v_is_active boolean;
    v_key_hash text;
    v_scopes jsonb;
    v_expires_at timestamptz;
BEGIN
    IF p_key_id IS NULL OR p_expected_hash IS NULL
       OR pg_catalog.octet_length(p_expected_hash) NOT BETWEEN 1 AND 255 THEN
        RETURN aap_identity_v1._admission_failure('invalid_request');
    END IF;

    SELECT key.user_id INTO v_actor_id
    FROM public.api_keys AS key
    WHERE key.id = p_key_id;
    IF NOT FOUND THEN
        RETURN aap_identity_v1._admission_failure('credential_invalid');
    END IF;

    BEGIN
        SELECT actor.id, actor.name, actor.email, actor.role, actor.is_active
        INTO v_user_id, v_name, v_email, v_role, v_is_active
        FROM public.users AS actor
        WHERE actor.id = v_actor_id
        FOR SHARE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_v1._admission_failure('credential_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_identity_v1._admission_failure('credential_invalid');
    END IF;

    BEGIN
        SELECT key.key_hash, key.scopes, key.expires_at
        INTO v_key_hash, v_scopes, v_expires_at
        FROM public.api_keys AS key
        WHERE key.id = p_key_id AND key.user_id = v_actor_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_v1._admission_failure('credential_busy');
    END;
    IF NOT FOUND OR v_key_hash IS DISTINCT FROM p_expected_hash THEN
        RETURN aap_identity_v1._admission_failure('credential_invalid');
    END IF;
    IF NOT v_is_active THEN
        RETURN aap_identity_v1._admission_failure('user_inactive');
    END IF;
    IF v_role IS NULL OR v_role NOT IN (
        'super_admin', 'project_admin', 'employee', 'viewer'
    ) THEN
        RETURN aap_identity_v1._admission_failure('invalid_account_role');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.api_keys AS key
        WHERE key.id = p_key_id AND key.revoked_at IS NOT NULL
    ) OR (v_expires_at IS NOT NULL AND v_expires_at <= pg_catalog.clock_timestamp()) THEN
        RETURN aap_identity_v1._admission_failure('credential_revoked');
    END IF;
    IF pg_catalog.jsonb_typeof(v_scopes) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid stored API key scopes';
    END IF;
    IF EXISTS (
           SELECT 1
           FROM pg_catalog.jsonb_array_elements(v_scopes) AS scope(value)
           WHERE pg_catalog.jsonb_typeof(scope.value) IS DISTINCT FROM 'string'
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid stored API key scopes';
    END IF;

    UPDATE public.api_keys AS key
    SET last_used_at = pg_catalog.clock_timestamp()
    WHERE key.id = p_key_id;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', v_user_id,
            'name', v_name,
            'email', v_email,
            'role', v_role,
            'is_active', true,
            'credential_type', 'api_key',
            'credential_id', p_key_id,
            'generation', NULL,
            'scopes', v_scopes,
            'expires_at', v_expires_at
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.confirm_api_key_principal(uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.get_credential_generation(p_actor_id uuid)
RETURNS bigint
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(
        (SELECT actor.credential_generation
         FROM public.users AS actor
         WHERE actor.id = p_actor_id),
        0::bigint
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.get_credential_generation(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.advance_credential_generation(p_actor_id uuid)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_generation bigint;
BEGIN
    IF p_actor_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;
    BEGIN
        SELECT actor.credential_generation
        INTO v_generation
        FROM public.users AS actor
        WHERE actor.id = p_actor_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'credential_busy';
    END;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'credential_invalid';
    END IF;
    IF v_generation = 9223372036854775807 THEN
        RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'credential generation exhausted';
    END IF;
    UPDATE public.users AS actor
    SET credential_generation = v_generation + 1
    WHERE actor.id = p_actor_id
    RETURNING actor.credential_generation INTO v_generation;
    RETURN v_generation;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.advance_credential_generation(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.revoke_jti(p_jti uuid, p_retain_until timestamptz)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
BEGIN
    IF p_jti IS NULL OR p_retain_until IS NULL
       OR NOT pg_catalog.isfinite(p_retain_until) THEN
        RETURN false;
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-identity-jti:' || p_jti::text, 0)
    );
    INSERT INTO public.identity_revoked_jtis AS stored (jti, expires_at, revoked_at)
    VALUES (p_jti, p_retain_until, pg_catalog.clock_timestamp())
    ON CONFLICT (jti) DO UPDATE
    SET expires_at = GREATEST(stored.expires_at, EXCLUDED.expires_at);
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.revoke_jti(uuid, timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.is_jti_revoked(p_jti uuid)
RETURNS boolean
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p_jti IS NOT NULL AND EXISTS (
        SELECT 1
        FROM public.identity_revoked_jtis AS revoked
        WHERE revoked.jti = p_jti
          AND revoked.expires_at > pg_catalog.clock_timestamp()
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.is_jti_revoked(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.retire_account_credentials(p_actor_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_generation bigint;
    v_revoked_key_ids uuid[];
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_actor_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;
    BEGIN
        SELECT actor.credential_generation
        INTO v_generation
        FROM public.users AS actor
        WHERE actor.id = p_actor_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'credential_busy';
    END;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'credential_invalid';
    END IF;
    IF v_generation = 9223372036854775807 THEN
        RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'credential generation exhausted';
    END IF;

    PERFORM key.id
    FROM public.api_keys AS key
    WHERE key.user_id = p_actor_id AND key.revoked_at IS NULL
    ORDER BY key.id
    FOR UPDATE;
    WITH revoked_keys AS (
        UPDATE public.api_keys AS key
        SET revoked_at = v_now
        WHERE key.user_id = p_actor_id AND key.revoked_at IS NULL
        RETURNING key.id
    )
    SELECT COALESCE(pg_catalog.array_agg(revoked_keys.id ORDER BY revoked_keys.id), ARRAY[]::uuid[])
    INTO v_revoked_key_ids
    FROM revoked_keys;

    PERFORM token.id
    FROM public.password_reset_tokens AS token
    WHERE token.user_id = p_actor_id AND token.used_at IS NULL
    ORDER BY token.id
    FOR UPDATE;
    UPDATE public.password_reset_tokens AS token
    SET used_at = v_now
    WHERE token.user_id = p_actor_id AND token.used_at IS NULL;

    UPDATE public.users AS actor
    SET credential_generation = v_generation + 1
    WHERE actor.id = p_actor_id
    RETURNING actor.credential_generation INTO v_generation;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-retirement/v1',
        'generation', v_generation,
        'revoked_key_ids', pg_catalog.to_jsonb(v_revoked_key_ids),
        'revoked_at', v_now
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.retire_account_credentials(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.lock_current_principal(
    p_actor_id uuid,
    p_kind text,
    p_credential_id uuid,
    p_generation bigint,
    p_expires_at timestamptz,
    p_required_scopes text[]
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
    v_user_id uuid;
    v_name text;
    v_email text;
    v_role text;
    v_is_active boolean;
    v_generation bigint;
    v_scopes jsonb;
    v_key_expiry timestamptz;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_actor_id IS NULL OR p_credential_id IS NULL OR p_kind IS NULL
       OR p_kind NOT IN ('jwt', 'api_key') THEN
        RETURN aap_identity_v1._admission_failure('invalid_request');
    END IF;
    IF p_kind = 'jwt' AND (
        p_generation IS NULL OR p_generation < 0 OR p_expires_at IS NULL
        OR NOT pg_catalog.isfinite(p_expires_at)
    ) THEN
        RETURN aap_identity_v1._admission_failure('invalid_request');
    END IF;
    IF p_kind = 'api_key' AND (
        p_generation IS NOT NULL OR p_expires_at IS NOT NULL
        OR p_required_scopes IS NULL
        OR pg_catalog.array_position(p_required_scopes, NULL::text) IS NOT NULL
    ) THEN
        RETURN aap_identity_v1._admission_failure('invalid_request');
    END IF;

    BEGIN
        SELECT actor.id, actor.name, actor.email, actor.role, actor.is_active,
               actor.credential_generation
        INTO v_user_id, v_name, v_email, v_role, v_is_active, v_generation
        FROM public.users AS actor
        WHERE actor.id = p_actor_id
        FOR SHARE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_v1._admission_failure('credential_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_identity_v1._admission_failure('credential_invalid');
    END IF;
    IF NOT v_is_active THEN
        RETURN aap_identity_v1._admission_failure('user_inactive');
    END IF;
    IF v_role IS NULL OR v_role NOT IN (
        'super_admin', 'project_admin', 'employee', 'viewer'
    ) THEN
        RETURN aap_identity_v1._admission_failure('invalid_account_role');
    END IF;

    IF p_kind = 'jwt' THEN
        IF v_generation IS DISTINCT FROM p_generation THEN
            RETURN aap_identity_v1._admission_failure('generation_outdated');
        END IF;
        IF p_expires_at <= v_now THEN
            RETURN aap_identity_v1._admission_failure('token_expired');
        END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock_shared(
            pg_catalog.hashtextextended('aap-identity-jti:' || p_credential_id::text, 0)
        ) THEN
            RETURN aap_identity_v1._admission_failure('credential_busy');
        END IF;
        IF aap_identity_v1.is_jti_revoked(p_credential_id) THEN
            RETURN aap_identity_v1._admission_failure('credential_revoked');
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'actor_id', v_user_id,
                'name', v_name,
                'email', v_email,
                'role', v_role,
                'is_active', true,
                'credential_type', 'jwt',
                'credential_id', p_credential_id,
                'generation', v_generation,
                'scopes', NULL,
                'expires_at', p_expires_at
            )
        );
    END IF;

    BEGIN
        SELECT key.scopes, key.expires_at
        INTO v_scopes, v_key_expiry
        FROM public.api_keys AS key
        WHERE key.id = p_credential_id AND key.user_id = p_actor_id
          AND key.revoked_at IS NULL
        FOR SHARE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_v1._admission_failure('credential_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_identity_v1._admission_failure('credential_revoked');
    END IF;
    IF v_key_expiry IS NOT NULL AND v_key_expiry <= v_now THEN
        RETURN aap_identity_v1._admission_failure('credential_revoked');
    END IF;
    IF pg_catalog.jsonb_typeof(v_scopes) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid stored API key scopes';
    END IF;
    IF EXISTS (
           SELECT 1
           FROM pg_catalog.jsonb_array_elements(v_scopes) AS scope(value)
           WHERE pg_catalog.jsonb_typeof(scope.value) IS DISTINCT FROM 'string'
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid stored API key scopes';
    END IF;
    IF NOT (v_scopes @> pg_catalog.to_jsonb(p_required_scopes))
       AND NOT (v_scopes @> '["*"]'::jsonb) THEN
        RETURN aap_identity_v1._admission_failure('credential_invalid');
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', v_user_id,
            'name', v_name,
            'email', v_email,
            'role', v_role,
            'is_active', true,
            'credential_type', 'api_key',
            'credential_id', p_credential_id,
            'generation', NULL,
            'scopes', v_scopes,
            'expires_at', v_key_expiry
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.lock_current_principal(
    uuid, text, uuid, bigint, timestamptz, text[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.purge_expired_revocations(p_limit integer)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted bigint;
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 10000 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;
    WITH expired AS (
        SELECT revoked.jti
        FROM public.identity_revoked_jtis AS revoked
        WHERE revoked.expires_at <= pg_catalog.clock_timestamp()
        ORDER BY revoked.expires_at, revoked.jti
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    )
    DELETE FROM public.identity_revoked_jtis AS revoked
    USING expired
    WHERE revoked.jti = expired.jti;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.purge_expired_revocations(integer)
    FROM PUBLIC;
