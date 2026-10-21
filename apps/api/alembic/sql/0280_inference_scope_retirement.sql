-- Durable, scope-specific backend retirement fences.
ALTER TABLE aap_inference_routing_v1.scopes
    ADD COLUMN admission_expires_at timestamptz;
-- aap-statement-break
-- Frozen 0279 tokens did not retain their individual exp values. Keep every
-- pre-upgrade scope fenced for one full admission-token lifetime from upgrade.
UPDATE aap_inference_routing_v1.scopes
SET admission_expires_at = pg_catalog.clock_timestamp() + interval '300 seconds';
-- aap-statement-break
ALTER TABLE aap_inference_routing_v1.scopes
    ALTER COLUMN admission_expires_at SET DEFAULT 'epoch'::timestamptz;
-- aap-statement-break
ALTER TABLE aap_inference_routing_v1.scopes
    ALTER COLUMN admission_expires_at SET NOT NULL;
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.scope_retirements (
    retirement_id uuid PRIMARY KEY,
    scope_id uuid NOT NULL UNIQUE
        REFERENCES aap_inference_routing_v1.scopes(id) ON DELETE RESTRICT,
    registry_id uuid NOT NULL,
    authority_epoch uuid NOT NULL,
    backend_incarnation uuid NOT NULL,
    challenge text NOT NULL CHECK (challenge COLLATE "C" ~ '^[0-9a-f]{64}$'),
    fence_until bigint NOT NULL CHECK (fence_until >= 0),
    admission_expires_at timestamptz NOT NULL,
    transport_identity_sha256 text NOT NULL CHECK (
        transport_identity_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    config_revision bigint NOT NULL CHECK (config_revision >= 1),
    realm text NOT NULL,
    phase text NOT NULL CHECK (phase IN ('prepared', 'draining', 'completed')),
    closed boolean NOT NULL DEFAULT true CHECK (closed IS TRUE),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now()
);
-- aap-statement-break
CREATE INDEX ix_aap_scope_retirements_open_registry
    ON aap_inference_routing_v1.scope_retirements (registry_id, created_at)
    WHERE phase IN ('prepared', 'draining');
-- aap-statement-break
REVOKE ALL ON aap_inference_routing_v1.scope_retirements FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_sign_admission(
    p_registry_id uuid,
    p_scope_id uuid,
    p_attempt_id uuid,
    p_operation text,
    p_method text,
    p_path text
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_state record;
    v_iat bigint;
    v_exp bigint;
    v_header text;
    v_payload text;
    v_unsigned text;
    v_signature text;
BEGIN
    SELECT state.registry_id, state.backend_incarnation, state.authority_epoch,
           state.epoch_key, state.realm, state.continuity
    INTO v_state
    FROM aap_inference_routing_v1.registry_state AS state
    WHERE state.registry_id = p_registry_id;
    IF NOT FOUND OR v_state.continuity IS DISTINCT FROM 'ready'
       OR v_state.epoch_key IS NULL OR v_state.backend_incarnation IS NULL
       OR v_state.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm() THEN
        RETURN NULL;
    END IF;
    v_iat := pg_catalog.floor(extract(epoch FROM pg_catalog.clock_timestamp()))::bigint;
    v_exp := v_iat + 300;
    v_header := aap_inference_routing_v1._routing_base64url(
        pg_catalog.convert_to('{"alg":"HS256","typ":"JWT"}', 'UTF8')
    );
    v_payload := aap_inference_routing_v1._routing_base64url(
        pg_catalog.convert_to(pg_catalog.jsonb_build_object(
            'schema', 'aap-route-admission/v1',
            'aud', 'ml-route',
            'registry_id', p_registry_id,
            'backend_incarnation', v_state.backend_incarnation,
            'authority_epoch', v_state.authority_epoch,
            'scope_id', p_scope_id,
            'attempt_id', p_attempt_id,
            'operation', p_operation,
            'method', p_method,
            'path', p_path,
            'iat', v_iat,
            'exp', v_exp
        )::text, 'UTF8')
    );
    v_unsigned := v_header || '.' || v_payload;
    v_signature := aap_inference_routing_v1._routing_base64url(
        public.hmac(pg_catalog.convert_to(v_unsigned, 'UTF8'), v_state.epoch_key, 'sha256')
    );
    -- p_scope_id is a regular admission scope for inference requests. Control
    -- JWTs use a control UUID, so their longer-lived accounting stays in the
    -- existing control protocol and does not touch this column.
    UPDATE aap_inference_routing_v1.scopes AS scope
    SET admission_expires_at = GREATEST(
            scope.admission_expires_at,
            pg_catalog.to_timestamp(v_exp::double precision)
        ),
        updated_at = pg_catalog.now()
    WHERE scope.id = p_scope_id
      AND scope.registry_id = p_registry_id;
    RETURN v_unsigned || '.' || v_signature;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_sign_admission(
    uuid, uuid, uuid, text, text, text
) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_has_unresolved(
    p_registry_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    UPDATE aap_inference_routing_v1.scopes AS scope
    SET state = 'expired', closed = true, updated_at = pg_catalog.now()
    WHERE scope.registry_id = p_registry_id
      AND scope.state = 'reserved'
      AND scope.heartbeat_deadline <= v_now
      AND NOT EXISTS (
          SELECT 1 FROM aap_inference_routing_v1.attempts AS attempt
          WHERE attempt.scope_id = scope.id
      );
    RETURN EXISTS (
        SELECT 1
        FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.registry_id = p_registry_id
          AND (scope.state IN ('active', 'uncertain')
               OR (scope.state = 'reserved' AND scope.heartbeat_deadline > v_now))
    ) OR EXISTS (
        SELECT 1
        FROM aap_inference_routing_v1.attempts AS attempt
        JOIN aap_inference_routing_v1.scopes AS scope ON scope.id = attempt.scope_id
        WHERE scope.registry_id = p_registry_id
          AND attempt.state IN ('active', 'uncertain')
    ) OR EXISTS (
        SELECT 1
        FROM aap_inference_routing_v1.scope_retirements AS retirement
        WHERE retirement.registry_id = p_registry_id
          AND retirement.phase IN ('prepared', 'draining')
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_has_unresolved(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_scope_retirement_guard_state()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF ROW(NEW.authority_epoch, NEW.backend_incarnation)
       IS DISTINCT FROM ROW(OLD.authority_epoch, OLD.backend_incarnation)
       AND EXISTS (
           SELECT 1
           FROM aap_inference_routing_v1.scope_retirements AS retirement
           WHERE retirement.registry_id = OLD.registry_id
             AND retirement.phase IN ('prepared', 'draining')
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '55P03',
            MESSAGE = 'scope retirement is unresolved',
            HINT = 'aap.inference.routing.management_busy';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._routing_scope_retirement_guard_state()
    FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_route_scope_retirement_guard_state
BEFORE UPDATE OF authority_epoch, backend_incarnation
ON aap_inference_routing_v1.registry_state
FOR EACH ROW
EXECUTE FUNCTION aap_inference_routing_v1._routing_scope_retirement_guard_state();
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_scope_retirement_guard_control()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF NEW.phase IN ('prepared', 'draining', 'uncertain')
       AND EXISTS (
           SELECT 1
           FROM aap_inference_routing_v1.scope_retirements AS retirement
           WHERE retirement.registry_id = NEW.registry_id
             AND retirement.phase IN ('prepared', 'draining')
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '55P03',
            MESSAGE = 'scope retirement is unresolved',
            HINT = 'aap.inference.routing.management_busy';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._routing_scope_retirement_guard_control()
    FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_route_scope_retirement_guard_control
BEFORE INSERT OR UPDATE OF kind, phase
ON aap_inference_routing_v1.controls
FOR EACH ROW
EXECUTE FUNCTION aap_inference_routing_v1._routing_scope_retirement_guard_control();
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._scope_retirement_result(
    p_status text,
    p_fields jsonb
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-scope-retirement-data/v1',
        'status', p_status
    ) || COALESCE(p_fields, '{}'::jsonb)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._scope_retirement_result(text, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._scope_retirement_rejected(
    p_reason text
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT aap_inference_routing_v1._scope_retirement_result(
        'rejected', pg_catalog.jsonb_build_object('reason', p_reason)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._scope_retirement_rejected(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._scope_retirement_view(
    p_retirement_id uuid
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'retirement_id', retirement.retirement_id,
        'scope_id', retirement.scope_id,
        'registry_id', retirement.registry_id,
        'authority_epoch', retirement.authority_epoch,
        'backend_incarnation', retirement.backend_incarnation,
        'phase', retirement.phase,
        'closed', true
    )
    FROM aap_inference_routing_v1.scope_retirements AS retirement
    WHERE retirement.retirement_id = p_retirement_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._scope_retirement_view(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._scope_retirement_context(
    p_retirement_id uuid
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'retirement_id', retirement.retirement_id,
        'scope_id', retirement.scope_id,
        'registry_id', retirement.registry_id,
        'authority_epoch', retirement.authority_epoch,
        'backend_incarnation', retirement.backend_incarnation,
        'challenge', retirement.challenge,
        'fence_until', retirement.fence_until,
        'endpoint', pg_catalog.jsonb_build_object(
            'url', registry.url,
            'auth_method', registry.auth_method,
            'auth_token', registry.auth_token
        )
    )
    FROM aap_inference_routing_v1.scope_retirements AS retirement
    JOIN public.ml_backend_registry AS registry
      ON registry.id = retirement.registry_id
    WHERE retirement.retirement_id = p_retirement_id
      AND retirement.phase IN ('prepared', 'draining')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._scope_retirement_context(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1.scope_retirement_step(
    p_action text,
    p_arguments text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_args jsonb;
    v_reply jsonb;
    v_action text := p_action;
    v_retirement_id uuid;
    v_scope_id uuid;
    v_registry_id uuid;
    v_reply_retirement_id uuid;
    v_reply_scope_id uuid;
    v_reply_registry_id uuid;
    v_reply_epoch uuid;
    v_reply_incarnation uuid;
    v_capability text;
    v_reply_phase text;
    v_active_requests bigint;
    v_fence_until bigint;
    v_transport_identity_sha256 text;
    v_transport jsonb;
    v_retirement record;
    v_scope record;
    v_state record;
    v_registry record;
    v_now timestamptz;
    v_found boolean;
BEGIN
    IF p_arguments IS NULL OR pg_catalog.octet_length(p_arguments) > 1048576 THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
    END IF;
    BEGIN
        v_args := p_arguments::jsonb;
    EXCEPTION WHEN data_exception THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
    END;
    IF pg_catalog.jsonb_typeof(v_args) IS DISTINCT FROM 'object' THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
    END IF;

    IF v_action = 'policy' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(v_args, ARRAY[]::text[]) THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
        END IF;
        RETURN aap_inference_routing_v1._scope_retirement_result('ok',
            pg_catalog.jsonb_build_object(
                'ready', aap_inference_routing_v1._routing_secret() IS NOT NULL,
                'functions', pg_catalog.jsonb_build_array(
                    'aap_inference_routing_v1.routing_policy()',
                    'aap_inference_routing_v1.routing_step(text,text)',
                    'aap_inference_routing_v1.scope_retirement_step(text,text)'
                )
            )
        );
    END IF;

    IF v_action IS NULL OR v_action NOT IN ('prepare', 'status', 'finish') THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
    END IF;
    IF (v_action IN ('prepare', 'status') AND NOT
        aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['retirement_id', 'scope_id', 'capability']
        )) OR (v_action = 'finish' AND NOT
        aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['retirement_id', 'scope_id', 'capability', 'reply']
        )) THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
    END IF;

    BEGIN
        v_retirement_id := aap_inference_routing_v1._routing_uuid(
            v_args ->> 'retirement_id'
        );
        v_scope_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'scope_id');
    EXCEPTION WHEN SQLSTATE '22023' THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
    END;
    v_capability := v_args ->> 'capability';
    IF pg_catalog.jsonb_typeof(v_args -> 'retirement_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_args -> 'scope_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_args -> 'capability') IS DISTINCT FROM 'string'
       OR v_capability !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
    END IF;

    IF v_action = 'status' THEN
        SELECT retirement.* INTO v_retirement
        FROM aap_inference_routing_v1.scope_retirements AS retirement
        WHERE retirement.retirement_id = v_retirement_id;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('scope_unavailable');
        END IF;
        IF v_retirement.scope_id IS DISTINCT FROM v_scope_id THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('retirement_conflict');
        END IF;
        IF v_retirement.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
           OR aap_inference_routing_v1._routing_scope_capability(v_scope_id)
                IS DISTINCT FROM v_capability THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('scope_unavailable');
        END IF;
        RETURN aap_inference_routing_v1._scope_retirement_result('ok',
            pg_catalog.jsonb_build_object(
                'retirement', aap_inference_routing_v1._scope_retirement_view(v_retirement_id),
                'context', NULL
            )
        );
    END IF;

    IF v_action = 'finish' THEN
        v_reply := v_args -> 'reply';
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_reply, ARRAY[
                'schema', 'retirement_id', 'registry_id', 'scope_id',
                'authority_epoch', 'backend_incarnation', 'challenge',
                'phase', 'active_requests'
            ]
        ) OR pg_catalog.jsonb_typeof(v_reply -> 'schema') IS DISTINCT FROM 'string'
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-backend-scope-retirement-reply/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'phase') IS DISTINCT FROM 'string'
           OR v_reply ->> 'phase' NOT IN ('draining', 'retired')
           OR pg_catalog.jsonb_typeof(v_reply -> 'challenge') IS DISTINCT FROM 'string'
           OR (v_reply ->> 'challenge') !~ '^[0-9a-f]{64}$' THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
        END IF;
        BEGIN
            v_reply_retirement_id := aap_inference_routing_v1._routing_uuid(
                v_reply ->> 'retirement_id'
            );
            v_reply_registry_id := aap_inference_routing_v1._routing_uuid(
                v_reply ->> 'registry_id'
            );
            v_reply_scope_id := aap_inference_routing_v1._routing_uuid(
                v_reply ->> 'scope_id'
            );
            v_reply_epoch := aap_inference_routing_v1._routing_uuid(
                v_reply ->> 'authority_epoch'
            );
            v_reply_incarnation := aap_inference_routing_v1._routing_uuid(
                v_reply ->> 'backend_incarnation'
            );
            v_active_requests := aap_inference_routing_v1._routing_integer(
                v_reply -> 'active_requests', 0, 2147483647
            );
        EXCEPTION WHEN SQLSTATE '22023' THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
        END;
        IF pg_catalog.jsonb_typeof(v_reply -> 'retirement_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'registry_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'scope_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'authority_epoch') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'backend_incarnation') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'active_requests') IS DISTINCT FROM 'number' THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
        END IF;
    END IF;

    -- Preliminary reads only discover lock keys. Both prepare and finish then
    -- acquire locks in registry -> state -> scope -> retirement order. The
    -- registry SHARE lock conflicts with pool/Registry management's UPDATE lock;
    -- the state lock fences epoch changes before any scope row is touched.
    SELECT scope.registry_id INTO v_registry_id
    FROM aap_inference_routing_v1.scopes AS scope
    WHERE scope.id = v_scope_id;
    IF NOT FOUND OR aap_inference_routing_v1._routing_scope_capability(v_scope_id)
        IS DISTINCT FROM v_capability THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('scope_unavailable');
    END IF;

    IF v_action = 'prepare' THEN
        SELECT retirement.* INTO v_retirement
        FROM aap_inference_routing_v1.scope_retirements AS retirement
        WHERE retirement.retirement_id = v_retirement_id;
        v_found := FOUND;
        IF v_found THEN
            IF v_retirement.scope_id IS DISTINCT FROM v_scope_id THEN
                RETURN aap_inference_routing_v1._scope_retirement_rejected('retirement_conflict');
            END IF;
            IF v_retirement.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm() THEN
                RETURN aap_inference_routing_v1._scope_retirement_rejected('scope_unavailable');
            END IF;
            IF v_retirement.phase = 'completed' THEN
                RETURN aap_inference_routing_v1._scope_retirement_result('ok',
                    pg_catalog.jsonb_build_object(
                        'retirement', aap_inference_routing_v1._scope_retirement_view(v_retirement_id),
                        'context', NULL
                    )
                );
            END IF;
        ELSE
            SELECT retirement.* INTO v_retirement
            FROM aap_inference_routing_v1.scope_retirements AS retirement
            WHERE retirement.scope_id = v_scope_id;
            IF FOUND THEN
                RETURN aap_inference_routing_v1._scope_retirement_rejected('retirement_conflict');
            END IF;
        END IF;
    END IF;

    IF v_action = 'finish' THEN
        SELECT retirement.* INTO v_retirement
        FROM aap_inference_routing_v1.scope_retirements AS retirement
        WHERE retirement.retirement_id = v_retirement_id;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('scope_unavailable');
        END IF;
        IF v_retirement.scope_id IS DISTINCT FROM v_scope_id
           OR v_retirement.registry_id IS DISTINCT FROM v_registry_id THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('retirement_conflict');
        END IF;
        IF v_reply_retirement_id IS DISTINCT FROM v_retirement.retirement_id
           OR v_reply_registry_id IS DISTINCT FROM v_retirement.registry_id
           OR v_reply_scope_id IS DISTINCT FROM v_retirement.scope_id
           OR v_reply_epoch IS DISTINCT FROM v_retirement.authority_epoch
           OR v_reply_incarnation IS DISTINCT FROM v_retirement.backend_incarnation
           OR v_reply ->> 'challenge' IS DISTINCT FROM v_retirement.challenge THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
        END IF;
        v_reply_phase := v_reply ->> 'phase';
        IF v_reply_phase = 'retired' AND v_active_requests <> 0 THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
        END IF;
        IF v_retirement.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm() THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('scope_unavailable');
        END IF;
        IF v_retirement.phase = 'completed' THEN
            IF v_reply_phase <> 'retired' OR v_active_requests <> 0 THEN
                RETURN aap_inference_routing_v1._scope_retirement_rejected('retirement_conflict');
            END IF;
            RETURN aap_inference_routing_v1._scope_retirement_result('ok',
                pg_catalog.jsonb_build_object(
                    'retirement', aap_inference_routing_v1._scope_retirement_view(v_retirement_id),
                    'context', NULL
                )
            );
        END IF;
    END IF;

    BEGIN
        SELECT registry.* INTO v_registry
        FROM public.ml_backend_registry AS registry
        WHERE registry.id = v_registry_id
        FOR SHARE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('transport_changed');
        END IF;
        SELECT state.* INTO v_state
        FROM aap_inference_routing_v1.registry_state AS state
        WHERE state.registry_id = v_registry_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('backend_not_ready');
        END IF;
        SELECT scope.* INTO v_scope
        FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.id = v_scope_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('scope_unavailable');
        END IF;
        IF v_action = 'finish' THEN
            SELECT retirement.* INTO v_retirement
            FROM aap_inference_routing_v1.scope_retirements AS retirement
            WHERE retirement.retirement_id = v_retirement_id
            FOR UPDATE NOWAIT;
            IF NOT FOUND THEN
                RETURN aap_inference_routing_v1._scope_retirement_rejected('scope_unavailable');
            END IF;
        ELSE
            SELECT retirement.* INTO v_retirement
            FROM aap_inference_routing_v1.scope_retirements AS retirement
            WHERE retirement.retirement_id = v_retirement_id
            FOR UPDATE NOWAIT;
            v_found := FOUND;
            IF NOT v_found THEN
                SELECT retirement.* INTO v_retirement
                FROM aap_inference_routing_v1.scope_retirements AS retirement
                WHERE retirement.scope_id = v_scope_id
                FOR UPDATE NOWAIT;
                v_found := FOUND;
            END IF;
        END IF;
    EXCEPTION WHEN lock_not_available THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('management_busy');
    END;

    IF v_scope.registry_id IS DISTINCT FROM v_registry_id
       OR v_scope.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
       OR aap_inference_routing_v1._routing_scope_capability(v_scope_id)
            IS DISTINCT FROM v_capability THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('scope_unavailable');
    END IF;
    IF v_state.realm IS DISTINCT FROM v_scope.realm
       OR v_state.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
       OR v_state.continuity IS DISTINCT FROM 'ready'
       OR v_state.backend_incarnation IS NULL THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('backend_not_ready');
    END IF;
    IF v_scope.config_revision IS DISTINCT FROM v_state.config_revision
       OR v_scope.authority_epoch IS DISTINCT FROM v_state.authority_epoch THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('transport_changed');
    END IF;
    v_transport := aap_inference_routing_v1._routing_transport_identity(v_registry_id);
    IF v_transport IS NULL THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('transport_changed');
    END IF;
    v_transport_identity_sha256 := pg_catalog.encode(
        public.digest(pg_catalog.convert_to(v_transport::text, 'UTF8'), 'sha256'), 'hex'
    );
    IF v_registry.auth_method IS DISTINCT FROM 'token'
       OR v_registry.auth_token IS NULL
       OR pg_catalog.char_length(v_registry.auth_token) NOT BETWEEN 1 AND 8192
       OR v_registry.auth_token !~ '^[!-~]+$'
       OR pg_catalog.jsonb_typeof(v_transport -> 'url') IS DISTINCT FROM 'string' THEN
        RETURN aap_inference_routing_v1._scope_retirement_rejected('backend_not_ready');
    END IF;
    IF v_action = 'prepare' THEN
        IF v_found THEN
            IF v_retirement.scope_id IS DISTINCT FROM v_scope_id
               OR v_retirement.retirement_id IS DISTINCT FROM v_retirement_id THEN
                RETURN aap_inference_routing_v1._scope_retirement_rejected('retirement_conflict');
            END IF;
            IF v_retirement.registry_id IS DISTINCT FROM v_registry_id
               OR v_retirement.authority_epoch IS DISTINCT FROM v_state.authority_epoch
               OR v_retirement.backend_incarnation IS DISTINCT FROM v_state.backend_incarnation
               OR v_retirement.config_revision IS DISTINCT FROM v_state.config_revision
               OR v_retirement.realm IS DISTINCT FROM v_state.realm
               OR v_retirement.transport_identity_sha256 IS DISTINCT FROM v_transport_identity_sha256 THEN
                RETURN aap_inference_routing_v1._scope_retirement_rejected('transport_changed');
            END IF;
            RETURN aap_inference_routing_v1._scope_retirement_result('ok',
                pg_catalog.jsonb_build_object(
                    'retirement', aap_inference_routing_v1._scope_retirement_view(v_retirement_id),
                    'context', CASE WHEN v_retirement.phase = 'completed' THEN NULL
                                    ELSE aap_inference_routing_v1._scope_retirement_context(v_retirement_id) END
                )
            );
        END IF;
        IF aap_inference_routing_v1._routing_has_control(v_registry_id) THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('management_busy');
        END IF;
        v_now := pg_catalog.clock_timestamp();
        v_fence_until := pg_catalog.ceil(extract(epoch FROM
            GREATEST(v_scope.admission_expires_at, v_now)
        ))::bigint;
        INSERT INTO aap_inference_routing_v1.scope_retirements (
            retirement_id, scope_id, registry_id, authority_epoch,
            backend_incarnation, challenge, fence_until, admission_expires_at,
            transport_identity_sha256, config_revision, realm, phase, closed
        ) VALUES (
            v_retirement_id, v_scope_id, v_registry_id, v_state.authority_epoch,
            v_state.backend_incarnation, pg_catalog.encode(public.gen_random_bytes(32), 'hex'),
            v_fence_until, v_scope.admission_expires_at,
            v_transport_identity_sha256, v_state.config_revision, v_state.realm,
            'prepared', true
        );
        UPDATE aap_inference_routing_v1.scopes AS scope
        SET closed = true,
            state = CASE WHEN scope.state = 'reserved' THEN 'active' ELSE scope.state END,
            updated_at = pg_catalog.now()
        WHERE scope.id = v_scope_id;
        RETURN aap_inference_routing_v1._scope_retirement_result('ok',
            pg_catalog.jsonb_build_object(
                'retirement', aap_inference_routing_v1._scope_retirement_view(v_retirement_id),
                'context', aap_inference_routing_v1._scope_retirement_context(v_retirement_id)
            )
        );
    END IF;

    IF v_action = 'finish' THEN
        IF v_retirement.scope_id IS DISTINCT FROM v_scope_id
           OR v_retirement.registry_id IS DISTINCT FROM v_registry_id
           OR v_retirement.realm IS DISTINCT FROM v_state.realm
           OR v_retirement.authority_epoch IS DISTINCT FROM v_state.authority_epoch
           OR v_retirement.backend_incarnation IS DISTINCT FROM v_state.backend_incarnation
           OR v_retirement.config_revision IS DISTINCT FROM v_state.config_revision
           OR v_retirement.transport_identity_sha256 IS DISTINCT FROM v_transport_identity_sha256 THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('transport_changed');
        END IF;
        IF v_state.continuity IS DISTINCT FROM 'ready'
           OR v_state.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
           OR v_scope.authority_epoch IS DISTINCT FROM v_retirement.authority_epoch
           OR v_scope.config_revision IS DISTINCT FROM v_retirement.config_revision
           OR v_scope.closed IS DISTINCT FROM true
           OR aap_inference_routing_v1._routing_has_control(v_registry_id) THEN
            RETURN aap_inference_routing_v1._scope_retirement_rejected('backend_not_ready');
        END IF;
        IF v_reply_phase = 'draining' THEN
            UPDATE aap_inference_routing_v1.scope_retirements AS retirement
            SET phase = 'draining', updated_at = pg_catalog.now()
            WHERE retirement.retirement_id = v_retirement_id
              AND retirement.phase IN ('prepared', 'draining');
        ELSE
            UPDATE aap_inference_routing_v1.attempts AS attempt
            SET state = 'reconciled', updated_at = pg_catalog.now()
            WHERE attempt.scope_id = v_scope_id
              AND attempt.state IN ('active', 'uncertain');
            UPDATE aap_inference_routing_v1.scopes AS scope
            SET state = 'reconciled', closed = true, reconciled_by = v_retirement_id,
                updated_at = pg_catalog.now()
            WHERE scope.id = v_scope_id;
            UPDATE aap_inference_routing_v1.scope_retirements AS retirement
            SET phase = 'completed', updated_at = pg_catalog.now()
            WHERE retirement.retirement_id = v_retirement_id
              AND retirement.phase IN ('prepared', 'draining');
        END IF;
        RETURN aap_inference_routing_v1._scope_retirement_result('ok',
            pg_catalog.jsonb_build_object(
                'retirement', aap_inference_routing_v1._scope_retirement_view(v_retirement_id),
                'context', CASE WHEN v_reply_phase = 'retired' THEN NULL
                                ELSE aap_inference_routing_v1._scope_retirement_context(v_retirement_id) END
            )
        );
    END IF;

    RETURN aap_inference_routing_v1._scope_retirement_rejected('invalid_command');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1.scope_retirement_step(text, text)
    FROM PUBLIC;
