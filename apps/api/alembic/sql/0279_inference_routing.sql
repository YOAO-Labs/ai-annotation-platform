-- Durable, caller-owned Inference routing authority. Redis is not proof of work state.
CREATE SCHEMA aap_inference_routing_v1;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_inference_routing_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.authority_secret (
    singleton boolean PRIMARY KEY CHECK (singleton),
    secret bytea NOT NULL CHECK (pg_catalog.octet_length(secret) = 32),
    realm_database_oid oid NOT NULL,
    realm_database_name text NOT NULL
);
-- aap-statement-break
INSERT INTO aap_inference_routing_v1.authority_secret (
    singleton, secret, realm_database_oid, realm_database_name
)
SELECT true, public.gen_random_bytes(32), database.oid, pg_catalog.current_database()
FROM pg_catalog.pg_database AS database
WHERE database.datname = pg_catalog.current_database();
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.registry_state (
    registry_id uuid PRIMARY KEY,
    config_revision bigint NOT NULL CHECK (config_revision >= 1),
    eligibility_revision bigint NOT NULL CHECK (eligibility_revision >= 1),
    authority_epoch uuid NOT NULL,
    backend_incarnation uuid,
    continuity text NOT NULL CHECK (continuity IN ('unverified', 'ready')),
    epoch_key bytea NOT NULL CHECK (pg_catalog.octet_length(epoch_key) = 32),
    realm text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now()
);
-- aap-statement-break
INSERT INTO aap_inference_routing_v1.registry_state (
    registry_id, config_revision, eligibility_revision, authority_epoch,
    backend_incarnation, continuity, epoch_key, realm
)
SELECT registry.id, 1, 1, public.gen_random_uuid(), NULL, 'unverified',
       public.gen_random_bytes(32),
       database.oid::text || ':' || database.datname
FROM public.ml_backend_registry AS registry
CROSS JOIN pg_catalog.pg_database AS database
WHERE database.datname = pg_catalog.current_database();
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.scopes (
    id uuid PRIMARY KEY,
    command_sha256 text NOT NULL CHECK (
        command_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    registry_id uuid NOT NULL,
    pool_id uuid,
    project_id uuid,
    mode text NOT NULL CHECK (mode IN ('off', 'observe', 'enforce', 'direct')),
    owner text NOT NULL CHECK (pg_catalog.char_length(owner) BETWEEN 1 AND 512),
    operation text NOT NULL CHECK (pg_catalog.char_length(operation) BETWEEN 1 AND 96),
    generation bigint,
    config_revision bigint NOT NULL CHECK (config_revision >= 1),
    eligibility_revision bigint NOT NULL CHECK (eligibility_revision >= 1),
    authority_epoch uuid NOT NULL,
    realm text NOT NULL,
    state text NOT NULL CHECK (
        state IN ('reserved', 'active', 'uncertain', 'finished', 'cancelled', 'expired', 'reconciled')
    ),
    closed boolean NOT NULL DEFAULT false,
    heartbeat_deadline timestamptz NOT NULL,
    reconciled_by uuid,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CHECK ((mode = 'direct' AND pool_id IS NULL AND generation IS NULL)
        OR (mode <> 'direct' AND pool_id IS NOT NULL AND generation IS NOT NULL))
);
-- aap-statement-break
CREATE INDEX ix_aap_routing_scopes_registry_state
    ON aap_inference_routing_v1.scopes (registry_id, state, heartbeat_deadline);
-- aap-statement-break
CREATE INDEX ix_aap_routing_scopes_pool_state
    ON aap_inference_routing_v1.scopes (pool_id, state, heartbeat_deadline);
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.attempts (
    id uuid PRIMARY KEY,
    scope_id uuid NOT NULL REFERENCES aap_inference_routing_v1.scopes(id) ON DELETE RESTRICT,
    method text NOT NULL CHECK (method ~ '^[A-Z]{1,16}$'),
    path text NOT NULL CHECK (pg_catalog.char_length(path) BETWEEN 1 AND 4096 AND path ~ '^/[^?#[:space:]]*$'),
    transport_identity_sha256 text NOT NULL CHECK (
        transport_identity_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    state text NOT NULL CHECK (state IN ('active', 'response', 'uncertain', 'reconciled')),
    status_code integer CHECK (status_code BETWEEN 100 AND 599),
    reason text CHECK (reason IS NULL OR reason ~ '^[a-z0-9_]{1,96}$'),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CHECK ((state = 'response' AND status_code IS NOT NULL)
        OR (state <> 'response' AND status_code IS NULL))
);
-- aap-statement-break
CREATE INDEX ix_aap_routing_attempts_scope_state
    ON aap_inference_routing_v1.attempts (scope_id, state);
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.weights (
    pool_id uuid NOT NULL,
    registry_id uuid NOT NULL,
    current_weight bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (pool_id, registry_id)
);
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.controls (
    id uuid PRIMARY KEY,
    registry_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('unload', 'reload', 'epoch')),
    phase text NOT NULL CHECK (phase IN ('prepared', 'draining', 'completed', 'uncertain')),
    new_epoch uuid,
    retired_epoch uuid,
    recovered_kind text CHECK (recovered_kind IN ('unload', 'reload')),
    challenge text NOT NULL CHECK (challenge COLLATE "C" ~ '^[0-9a-f]{64}$'),
    backend_incarnation uuid,
    config_revision bigint NOT NULL CHECK (config_revision >= 1),
    realm text NOT NULL,
    expected_topology jsonb,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CHECK ((kind = 'epoch' AND new_epoch IS NOT NULL)
        OR (kind <> 'epoch' AND new_epoch IS NULL)),
    CHECK (kind = 'epoch' OR retired_epoch IS NULL),
    CHECK (recovered_kind IS NULL OR kind = 'epoch')
);
-- aap-statement-break
CREATE UNIQUE INDEX uq_aap_routing_controls_open_registry
    ON aap_inference_routing_v1.controls (registry_id)
    WHERE phase IN ('prepared', 'draining', 'uncertain');
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.control_attempts (
    id uuid PRIMARY KEY,
    control_id uuid NOT NULL REFERENCES aap_inference_routing_v1.controls(id) ON DELETE RESTRICT,
    method text NOT NULL CHECK (method ~ '^[A-Z]{1,16}$'),
    path text NOT NULL CHECK (pg_catalog.char_length(path) BETWEEN 1 AND 4096 AND path ~ '^/[^?#[:space:]]*$'),
    transport_identity_sha256 text NOT NULL CHECK (
        transport_identity_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    state text NOT NULL CHECK (state IN ('active', 'response', 'uncertain', 'reconciled')),
    status_code integer CHECK (status_code BETWEEN 100 AND 599),
    reason text CHECK (reason IS NULL OR reason ~ '^[a-z0-9_]{1,96}$'),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CHECK ((state = 'response' AND status_code IS NOT NULL)
        OR (state <> 'response' AND status_code IS NULL))
);
-- aap-statement-break
CREATE INDEX ix_aap_routing_control_attempts_control_state
    ON aap_inference_routing_v1.control_attempts (control_id, state);
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.command_receipts (
    id uuid PRIMARY KEY,
    command_sha256 text NOT NULL CHECK (
        command_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    operation text NOT NULL CHECK (
        operation IN ('acquire', 'begin', 'settle', 'heartbeat', 'close', 'scope_status',
                      'reconcile', 'prepare_control', 'finish_control',
                      'begin_control', 'settle_control')
    ),
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    creating_xid xid8 NOT NULL,
    realm text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
REVOKE ALL ON ALL TABLES IN SCHEMA aap_inference_routing_v1 FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_exact_keys(
    p_value jsonb,
    p_keys text[]
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT CASE
        WHEN pg_catalog.jsonb_typeof(p_value) = 'object' THEN
            (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_value))
                = pg_catalog.cardinality(p_keys)
            AND p_value ?& p_keys
        ELSE false
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_exact_keys(jsonb, text[])
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_uuid(p_value text)
RETURNS uuid
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_uuid uuid;
BEGIN
    IF p_value IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing identifier';
    END IF;
    BEGIN
        v_uuid := p_value::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing identifier';
    END;
    IF v_uuid::text IS DISTINCT FROM p_value THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing identifier';
    END IF;
    RETURN v_uuid;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_uuid(text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_integer(
    p_value jsonb,
    p_min bigint,
    p_max bigint
)
RETURNS bigint
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_text text;
    v_value bigint;
BEGIN
    IF pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing integer';
    END IF;
    v_text := p_value #>> '{}';
    IF v_text !~ '^-?(0|[1-9][0-9]*)$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing integer';
    END IF;
    BEGIN
        v_value := v_text::bigint;
    EXCEPTION WHEN numeric_value_out_of_range OR invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing integer';
    END;
    IF v_value < p_min OR v_value > p_max THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing integer';
    END IF;
    RETURN v_value;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_integer(jsonb, bigint, bigint)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_reply(
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
        'schema', 'aap-inference-routing-data/v1',
        'status', p_status
    ) || COALESCE(p_fields, '{}'::jsonb)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_reply(text, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_rejected(p_reason text)
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
    SELECT aap_inference_routing_v1._routing_reply(
        'rejected', pg_catalog.jsonb_build_object('reason', p_reason)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_rejected(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_receipt_safe(
    p_value jsonb
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_pair record;
    v_item jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_value) = 'object' THEN
        FOR v_pair IN SELECT object.key, object.value
                      FROM pg_catalog.jsonb_each(p_value) AS object(key, value)
        LOOP
            IF v_pair.key = 'headers' THEN
                IF v_pair.value = '{}'::jsonb OR (
                    aap_inference_routing_v1._routing_exact_keys(
                        v_pair.value, ARRAY['X-AAP-Route-Admission']
                    )
                    AND pg_catalog.jsonb_typeof(v_pair.value -> 'X-AAP-Route-Admission') = 'string'
                    AND pg_catalog.char_length(v_pair.value ->> 'X-AAP-Route-Admission') <= 16384
                    AND (v_pair.value ->> 'X-AAP-Route-Admission') COLLATE "C"
                        ~ '^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$'
                ) THEN
                    CONTINUE;
                END IF;
                RETURN false;
            END IF;
            IF pg_catalog.lower(v_pair.key) = ANY(ARRAY[
                'url', 'auth_method', 'auth_token', 'extra_params', 'gpu_resource_id',
                'health_meta', 'endpoint', 'signing_key', 'epoch_key',
                'transport_identity', 'subject', 'headers', 'probe'
            ]) OR NOT aap_inference_routing_v1._routing_receipt_safe(v_pair.value) THEN
                RETURN false;
            END IF;
        END LOOP;
    ELSIF pg_catalog.jsonb_typeof(p_value) = 'array' THEN
        FOR v_item IN SELECT element.value
                      FROM pg_catalog.jsonb_array_elements(p_value) AS element(value)
        LOOP
            IF NOT aap_inference_routing_v1._routing_receipt_safe(v_item) THEN
                RETURN false;
            END IF;
        END LOOP;
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_receipt_safe(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_realm()
RETURNS text
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT database.oid::text || ':' || database.datname
    FROM pg_catalog.pg_database AS database
    WHERE database.datname = pg_catalog.current_database()
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_realm() FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_secret()
RETURNS bytea
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT secret.secret
    FROM aap_inference_routing_v1.authority_secret AS secret
    JOIN pg_catalog.pg_database AS database
      ON database.oid = secret.realm_database_oid
     AND database.datname = secret.realm_database_name
    WHERE secret.singleton IS TRUE
      AND database.datname = pg_catalog.current_database()
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_secret() FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_base64url(p_value bytea)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT pg_catalog.translate(
        pg_catalog.rtrim(pg_catalog.replace(pg_catalog.encode(p_value, 'base64'), E'\n', ''), '='),
        '+/', '-_'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_base64url(bytea)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_unbase64url(p_value text)
RETURNS bytea
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_b64 text;
BEGIN
    IF p_value IS NULL OR p_value !~ '^[A-Za-z0-9_-]+$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing token';
    END IF;
    v_b64 := pg_catalog.translate(p_value, '-_', '+/');
    v_b64 := v_b64 || pg_catalog.repeat('=', (4 - pg_catalog.length(v_b64) % 4) % 4);
    BEGIN
        RETURN pg_catalog.decode(v_b64, 'base64');
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing token';
    END;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_unbase64url(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_sign_snapshot(
    p_payload jsonb
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
    v_secret bytea;
    v_payload text;
    v_payload64 text;
    v_signature text;
BEGIN
    v_secret := aap_inference_routing_v1._routing_secret();
    IF v_secret IS NULL OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'routing authority is unavailable';
    END IF;
    v_payload := p_payload::text;
    v_payload64 := aap_inference_routing_v1._routing_base64url(
        pg_catalog.convert_to(v_payload, 'UTF8')
    );
    v_signature := pg_catalog.encode(
        public.hmac(pg_catalog.convert_to(v_payload64, 'UTF8'), v_secret, 'sha256'),
        'hex'
    );
    RETURN 'v1.' || v_payload64 || '.' || v_signature;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_sign_snapshot(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_open_snapshot(
    p_token text
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
    v_parts text[];
    v_secret bytea;
    v_payload text;
    v_signature text;
    v_expected text;
    v_value jsonb;
BEGIN
    IF p_token IS NULL OR pg_catalog.octet_length(p_token) > 1048576 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing snapshot';
    END IF;
    v_parts := pg_catalog.string_to_array(p_token, '.');
    IF pg_catalog.cardinality(v_parts) <> 3 OR v_parts[1] <> 'v1'
       OR v_parts[3] !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing snapshot';
    END IF;
    v_secret := aap_inference_routing_v1._routing_secret();
    IF v_secret IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'routing authority is unavailable';
    END IF;
    v_expected := pg_catalog.encode(
        public.hmac(pg_catalog.convert_to(v_parts[2], 'UTF8'), v_secret, 'sha256'),
        'hex'
    );
    IF v_expected IS DISTINCT FROM v_parts[3] THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing snapshot';
    END IF;
    BEGIN
        v_payload := pg_catalog.convert_from(
            aap_inference_routing_v1._routing_unbase64url(v_parts[2]), 'UTF8'
        );
        v_value := v_payload::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing snapshot';
    END;
    IF pg_catalog.jsonb_typeof(v_value) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing snapshot';
    END IF;
    RETURN v_value;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_open_snapshot(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_scope_capability(
    p_scope_id uuid
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
    v_scope record;
    v_secret bytea;
    v_payload text;
BEGIN
    SELECT scope.id, scope.registry_id, scope.pool_id, scope.project_id,
           scope.mode, scope.owner, scope.operation, scope.generation,
           scope.config_revision, scope.eligibility_revision,
           scope.authority_epoch, scope.realm
    INTO v_scope
    FROM aap_inference_routing_v1.scopes AS scope
    WHERE scope.id = p_scope_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    v_secret := aap_inference_routing_v1._routing_secret();
    IF v_secret IS NULL OR v_scope.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm() THEN
        RETURN NULL;
    END IF;
    v_payload := pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-routing-capability/v1',
        'database', v_scope.realm,
        'id', v_scope.id,
        'registry_id', v_scope.registry_id,
        'pool_id', v_scope.pool_id,
        'project_id', v_scope.project_id,
        'mode', v_scope.mode,
        'owner', v_scope.owner,
        'operation', v_scope.operation,
        'generation', v_scope.generation,
        'config_revision', v_scope.config_revision,
        'eligibility_revision', v_scope.eligibility_revision,
        'authority_epoch', v_scope.authority_epoch
    )::text;
    RETURN pg_catalog.encode(
        public.hmac(pg_catalog.convert_to(v_payload, 'UTF8'), v_secret, 'sha256'),
        'hex'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_scope_capability(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_control_capability(
    p_control_id uuid
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
    v_control record;
    v_secret bytea;
    v_payload text;
BEGIN
    SELECT control.id, control.registry_id, control.kind,
           control.new_epoch, control.retired_epoch, control.challenge,
           control.config_revision, control.realm
    INTO v_control
    FROM aap_inference_routing_v1.controls AS control
    WHERE control.id = p_control_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    v_secret := aap_inference_routing_v1._routing_secret();
    IF v_secret IS NULL OR v_control.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm() THEN
        RETURN NULL;
    END IF;
    v_payload := pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-routing-control-capability/v1',
        'database', v_control.realm,
        'id', v_control.id,
        'registry_id', v_control.registry_id,
        'kind', v_control.kind,
        'new_epoch', v_control.new_epoch,
        'retired_epoch', v_control.retired_epoch,
        'challenge', v_control.challenge,
        'config_revision', v_control.config_revision
    )::text;
    RETURN pg_catalog.encode(
        public.hmac(pg_catalog.convert_to(v_payload, 'UTF8'), v_secret, 'sha256'),
        'hex'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_control_capability(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_scope_view(
    p_scope_id uuid,
    p_include_capability boolean
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
    v_scope record;
    v_result jsonb;
BEGIN
    SELECT scope.id, scope.pool_id, scope.registry_id, scope.project_id,
           scope.mode, scope.owner, scope.operation, scope.generation,
           scope.config_revision, scope.eligibility_revision,
           scope.authority_epoch, scope.state, scope.closed,
           scope.heartbeat_deadline
    INTO v_scope
    FROM aap_inference_routing_v1.scopes AS scope
    WHERE scope.id = p_scope_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    v_result := pg_catalog.jsonb_build_object(
        'id', v_scope.id,
        'pool_id', v_scope.pool_id,
        'registry_id', v_scope.registry_id,
        'project_id', v_scope.project_id,
        'mode', v_scope.mode,
        'owner', v_scope.owner,
        'operation', v_scope.operation,
        'generation', v_scope.generation,
        'config_revision', v_scope.config_revision,
        'eligibility_revision', v_scope.eligibility_revision,
        'authority_epoch', v_scope.authority_epoch,
        'state', v_scope.state,
        'closed', v_scope.closed,
        'heartbeat_deadline_ms',
            pg_catalog.floor(extract(epoch FROM v_scope.heartbeat_deadline) * 1000)::bigint
    );
    IF p_include_capability THEN
        v_result := v_result || pg_catalog.jsonb_build_object(
            'capability', aap_inference_routing_v1._routing_scope_capability(p_scope_id)
        );
    END IF;
    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_scope_view(uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_control_view(
    p_control_id uuid,
    p_include_capability boolean
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
    v_control record;
    v_result jsonb;
BEGIN
    SELECT control.id, control.registry_id, control.kind, control.phase,
           control.new_epoch, control.backend_incarnation, control.config_revision
    INTO v_control
    FROM aap_inference_routing_v1.controls AS control
    WHERE control.id = p_control_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    v_result := pg_catalog.jsonb_build_object(
        'id', v_control.id,
        'registry_id', v_control.registry_id,
        'kind', v_control.kind,
        'phase', v_control.phase,
        'new_epoch', v_control.new_epoch,
        'backend_incarnation', v_control.backend_incarnation,
        'config_revision', v_control.config_revision
    );
    IF p_include_capability THEN
        v_result := v_result || pg_catalog.jsonb_build_object(
            'capability', aap_inference_routing_v1._routing_control_capability(p_control_id)
        );
    END IF;
    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_control_view(uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_transport_identity(
    p_registry_id uuid
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
        'url', registry.url,
        'auth_method', registry.auth_method,
        'auth_token', registry.auth_token,
        'gpu_resource_id', registry.gpu_resource_id,
        'extra_params', registry.extra_params
    )
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_transport_identity(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_max_concurrency(
    p_registry_id uuid
)
RETURNS integer
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_max integer;
    v_json jsonb;
    v_text text;
BEGIN
    SELECT membership.max_concurrency
    INTO v_max
    FROM public.gpu_backend_memberships AS membership
    WHERE membership.backend_registry_id = p_registry_id
      AND membership.state IN ('pending', 'active');
    IF FOUND AND v_max > 0 THEN
        RETURN v_max;
    END IF;
    SELECT registry.extra_params -> 'max_concurrency'
    INTO v_json
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id;
    IF pg_catalog.jsonb_typeof(v_json) = 'number' THEN
        v_text := v_json #>> '{}';
        IF v_text ~ '^[1-9][0-9]*$' THEN
            BEGIN
                v_max := v_text::integer;
            EXCEPTION WHEN numeric_value_out_of_range OR invalid_text_representation THEN
                v_max := NULL;
            END;
            IF v_max IS NOT NULL AND v_max > 0 THEN
                RETURN v_max;
            END IF;
        END IF;
    END IF;
    RETURN 4;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_max_concurrency(uuid)
    FROM PUBLIC;
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
            'exp', v_iat + 300
        )::text, 'UTF8')
    );
    v_unsigned := v_header || '.' || v_payload;
    v_signature := aap_inference_routing_v1._routing_base64url(
        public.hmac(pg_catalog.convert_to(v_unsigned, 'UTF8'), v_state.epoch_key, 'sha256')
    );
    RETURN v_unsigned || '.' || v_signature;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_sign_admission(
    uuid, uuid, uuid, text, text, text
) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_lock_admission_pool(
    p_pool_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
SET DateStyle = 'ISO, YMD'
SET extra_float_digits = 3
SET lock_timeout = '250ms'
AS $function$
BEGIN
    -- Queue short admissions fairly without conflicting with pending FK references.
    PERFORM pool.id
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR NO KEY UPDATE;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_lock_admission_pool(uuid)
FROM PUBLIC;
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
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_has_unresolved(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_has_control(
    p_registry_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM aap_inference_routing_v1.controls AS control
        WHERE control.registry_id = p_registry_id
          AND control.phase IN ('prepared', 'draining', 'uncertain')
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_has_control(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_registry_after_insert()
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
DECLARE
    v_realm text := aap_inference_routing_v1._routing_realm();
BEGIN
    IF v_realm IS NULL OR aap_inference_routing_v1._routing_secret() IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'routing authority is unavailable';
    END IF;
    INSERT INTO aap_inference_routing_v1.registry_state (
        registry_id, config_revision, eligibility_revision, authority_epoch,
        backend_incarnation, continuity, epoch_key, realm, updated_at
    ) VALUES (
        NEW.id, 1, 1, public.gen_random_uuid(), NULL, 'unverified',
        public.gen_random_bytes(32), v_realm, pg_catalog.now()
    )
    ON CONFLICT (registry_id) DO UPDATE
    SET config_revision = aap_inference_routing_v1.registry_state.config_revision + 1,
        eligibility_revision = aap_inference_routing_v1.registry_state.eligibility_revision + 1,
        authority_epoch = public.gen_random_uuid(),
        backend_incarnation = NULL,
        continuity = 'unverified',
        epoch_key = public.gen_random_bytes(32),
        realm = EXCLUDED.realm,
        updated_at = pg_catalog.now();
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_registry_after_insert()
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_registry_before_change()
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
DECLARE
    v_pool_id uuid;
    v_config_changed boolean;
    v_eligibility_changed boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        FOR v_pool_id IN
            SELECT DISTINCT member.pool_id
            FROM public.ml_backend_pool_members AS member
            WHERE member.registry_id = OLD.id
            ORDER BY member.pool_id
        LOOP
            BEGIN
                PERFORM pool.id
                FROM public.ml_backend_service_pools AS pool
                WHERE pool.id = v_pool_id
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55P03',
                    MESSAGE = 'routing management is busy',
                    HINT = 'aap.inference.routing.management_busy';
            END;
        END LOOP;
        IF aap_inference_routing_v1._routing_has_unresolved(OLD.id)
           OR aap_inference_routing_v1._routing_has_control(OLD.id) THEN
            RAISE EXCEPTION USING
                ERRCODE = '55P03',
                MESSAGE = 'routing management is busy',
                HINT = 'aap.inference.routing.management_busy';
        END IF;
        RETURN OLD;
    END IF;

    v_config_changed := ROW(
        OLD.url, OLD.auth_method, OLD.auth_token, OLD.extra_params,
        OLD.gpu_resource_id, OLD.vram_budget_mb, OLD.eviction_priority
    ) IS DISTINCT FROM ROW(
        NEW.url, NEW.auth_method, NEW.auth_token, NEW.extra_params,
        NEW.gpu_resource_id, NEW.vram_budget_mb, NEW.eviction_priority
    );
    v_eligibility_changed := ROW(
        OLD.state, OLD.is_interactive, OLD.health_meta, OLD.last_checked_at,
        OLD.error_message
    ) IS DISTINCT FROM ROW(
        NEW.state, NEW.is_interactive, NEW.health_meta, NEW.last_checked_at,
        NEW.error_message
    );

    IF v_config_changed THEN
        FOR v_pool_id IN
            SELECT DISTINCT member.pool_id
            FROM public.ml_backend_pool_members AS member
            WHERE member.registry_id = OLD.id
            ORDER BY member.pool_id
        LOOP
            BEGIN
                PERFORM pool.id
                FROM public.ml_backend_service_pools AS pool
                WHERE pool.id = v_pool_id
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55P03',
                    MESSAGE = 'routing management is busy',
                    HINT = 'aap.inference.routing.management_busy';
            END;
        END LOOP;
        IF aap_inference_routing_v1._routing_has_unresolved(OLD.id)
           OR aap_inference_routing_v1._routing_has_control(OLD.id) THEN
            RAISE EXCEPTION USING
                ERRCODE = '55P03',
                MESSAGE = 'routing management is busy',
                HINT = 'aap.inference.routing.management_busy';
        END IF;
    END IF;

    INSERT INTO aap_inference_routing_v1.registry_state (
        registry_id, config_revision, eligibility_revision, authority_epoch,
        backend_incarnation, continuity, epoch_key, realm
    ) VALUES (
        OLD.id, 1, 1, public.gen_random_uuid(), NULL, 'unverified',
        public.gen_random_bytes(32), aap_inference_routing_v1._routing_realm()
    ) ON CONFLICT (registry_id) DO NOTHING;

    IF v_config_changed OR v_eligibility_changed THEN
        UPDATE aap_inference_routing_v1.registry_state AS state
        SET config_revision = state.config_revision + CASE WHEN v_config_changed THEN 1 ELSE 0 END,
            eligibility_revision = state.eligibility_revision + CASE WHEN v_eligibility_changed THEN 1 ELSE 0 END,
            updated_at = pg_catalog.now()
        WHERE state.registry_id = OLD.id;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_registry_before_change()
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_pool_before_change()
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
DECLARE
    v_registry_id uuid;
    v_configuration_changed boolean;
    v_routing_changed boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        FOR v_registry_id IN
            SELECT member.registry_id
            FROM public.ml_backend_pool_members AS member
            WHERE member.pool_id = OLD.id
            ORDER BY member.registry_id
        LOOP
            IF aap_inference_routing_v1._routing_has_unresolved(v_registry_id)
               OR aap_inference_routing_v1._routing_has_control(v_registry_id) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55P03',
                    MESSAGE = 'routing management is busy',
                    HINT = 'aap.inference.routing.management_busy';
            END IF;
        END LOOP;
        DELETE FROM aap_inference_routing_v1.weights AS weight
        WHERE weight.pool_id = OLD.id;
        RETURN OLD;
    END IF;
    v_configuration_changed := ROW(
        OLD.routing_policy, OLD.legacy_instance_id
    ) IS DISTINCT FROM ROW(
        NEW.routing_policy, NEW.legacy_instance_id
    );
    v_routing_changed := v_configuration_changed
        OR ROW(OLD.capability_fingerprint, OLD.capability_snapshot, OLD.enabled)
           IS DISTINCT FROM ROW(NEW.capability_fingerprint, NEW.capability_snapshot, NEW.enabled);

    IF v_configuration_changed THEN
        FOR v_registry_id IN
            SELECT member.registry_id
            FROM public.ml_backend_pool_members AS member
            WHERE member.pool_id = OLD.id
            ORDER BY member.registry_id
        LOOP
            IF aap_inference_routing_v1._routing_has_unresolved(v_registry_id)
               OR aap_inference_routing_v1._routing_has_control(v_registry_id) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55P03',
                    MESSAGE = 'routing management is busy',
                    HINT = 'aap.inference.routing.management_busy';
            END IF;
        END LOOP;
    END IF;
    IF NEW.enabled IS TRUE AND OLD.enabled IS DISTINCT FROM TRUE THEN
        FOR v_registry_id IN
            SELECT member.registry_id
            FROM public.ml_backend_pool_members AS member
            WHERE member.pool_id = OLD.id
            ORDER BY member.registry_id
        LOOP
            IF aap_inference_routing_v1._routing_has_control(v_registry_id) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55P03',
                    MESSAGE = 'routing management is busy',
                    HINT = 'aap.inference.routing.management_busy';
            END IF;
        END LOOP;
    END IF;
    IF v_routing_changed AND NEW.routing_generation <= OLD.routing_generation THEN
        NEW.routing_generation := OLD.routing_generation + 1;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_pool_before_change()
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_pool_after_generation()
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
    IF NEW.routing_generation IS DISTINCT FROM OLD.routing_generation THEN
        DELETE FROM aap_inference_routing_v1.weights AS weight
        WHERE weight.pool_id = NEW.id;
        UPDATE aap_inference_routing_v1.registry_state AS state
        SET eligibility_revision = state.eligibility_revision + 1,
            updated_at = pg_catalog.now()
        WHERE state.registry_id IN (
            SELECT member.registry_id
            FROM public.ml_backend_pool_members AS member
            WHERE member.pool_id = NEW.id
        );
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_pool_after_generation()
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_member_before_change()
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
DECLARE
    v_pool_id uuid;
    v_registry_id uuid;
    v_identity_changed boolean := false;
    v_configuration_changed boolean := false;
    v_state_changed boolean := false;
    v_pool_ids uuid[];
    v_registry_ids uuid[];
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_pool_ids := ARRAY[NEW.pool_id];
        v_registry_ids := ARRAY[NEW.registry_id];
    ELSIF TG_OP = 'DELETE' THEN
        v_pool_ids := ARRAY[OLD.pool_id];
        v_registry_ids := ARRAY[OLD.registry_id];
    ELSE
        v_pool_ids := ARRAY[OLD.pool_id, NEW.pool_id];
        v_registry_ids := ARRAY[OLD.registry_id, NEW.registry_id];
        v_identity_changed := ROW(OLD.pool_id, OLD.registry_id)
            IS DISTINCT FROM ROW(NEW.pool_id, NEW.registry_id);
        v_configuration_changed := OLD.weight IS DISTINCT FROM NEW.weight;
        v_state_changed := OLD.traffic_state IS DISTINCT FROM NEW.traffic_state;
    END IF;

    FOR v_pool_id IN
        SELECT DISTINCT value
        FROM pg_catalog.unnest(v_pool_ids) AS ids(value)
        WHERE value IS NOT NULL
        ORDER BY value
    LOOP
        BEGIN
            PERFORM pool.id
            FROM public.ml_backend_service_pools AS pool
            WHERE pool.id = v_pool_id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available THEN
            RAISE EXCEPTION USING
                ERRCODE = '55P03',
                MESSAGE = 'routing management is busy',
                HINT = 'aap.inference.routing.management_busy';
        END;
    END LOOP;

    IF TG_OP = 'DELETE' OR v_identity_changed OR v_configuration_changed THEN
        FOR v_registry_id IN
            SELECT DISTINCT value
            FROM pg_catalog.unnest(v_registry_ids) AS ids(value)
            WHERE value IS NOT NULL
            ORDER BY value
        LOOP
            IF aap_inference_routing_v1._routing_has_unresolved(v_registry_id)
               OR aap_inference_routing_v1._routing_has_control(v_registry_id) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55P03',
                    MESSAGE = 'routing management is busy',
                    HINT = 'aap.inference.routing.management_busy';
            END IF;
        END LOOP;
    ELSIF v_state_changed THEN
        IF NEW.traffic_state = 'active' THEN
            IF aap_inference_routing_v1._routing_has_control(NEW.registry_id) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55P03',
                    MESSAGE = 'routing management is busy',
                    HINT = 'aap.inference.routing.management_busy';
            END IF;
        ELSIF TG_OP = 'UPDATE'
              AND aap_inference_routing_v1._routing_has_control(OLD.registry_id) THEN
            RAISE EXCEPTION USING
                ERRCODE = '55P03',
                MESSAGE = 'routing management is busy',
                HINT = 'aap.inference.routing.management_busy';
        END IF;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.traffic_state = 'active'
       AND aap_inference_routing_v1._routing_has_control(NEW.registry_id) THEN
        RAISE EXCEPTION USING
            ERRCODE = '55P03',
            MESSAGE = 'routing management is busy',
            HINT = 'aap.inference.routing.management_busy';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_member_before_change()
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_project_pool_before_change()
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
DECLARE
    v_pool_ids uuid[];
    v_pool_id uuid;
    v_changed boolean := true;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_pool_ids := ARRAY[NEW.pool_id];
    ELSIF TG_OP = 'DELETE' THEN
        v_pool_ids := ARRAY[OLD.pool_id];
    ELSE
        v_pool_ids := ARRAY[OLD.pool_id, NEW.pool_id];
        v_changed := ROW(OLD.project_id, OLD.pool_id, OLD.enabled, OLD.default_variants)
            IS DISTINCT FROM ROW(NEW.project_id, NEW.pool_id, NEW.enabled, NEW.default_variants);
    END IF;
    IF NOT v_changed THEN
        RETURN NEW;
    END IF;

    FOR v_pool_id IN
        SELECT DISTINCT value
        FROM pg_catalog.unnest(v_pool_ids) AS ids(value)
        WHERE value IS NOT NULL
        ORDER BY value
    LOOP
        BEGIN
            PERFORM pool.id
            FROM public.ml_backend_service_pools AS pool
            WHERE pool.id = v_pool_id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available THEN
            RAISE EXCEPTION USING
                ERRCODE = '55P03',
                MESSAGE = 'routing management is busy',
                HINT = 'aap.inference.routing.management_busy';
        END;
        IF EXISTS (
            SELECT 1
            FROM public.ml_backend_pool_members AS member
            JOIN aap_inference_routing_v1.controls AS control
              ON control.registry_id = member.registry_id
            WHERE member.pool_id = v_pool_id
              AND control.phase IN ('prepared', 'draining', 'uncertain')
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '55P03',
                MESSAGE = 'routing management is busy',
                HINT = 'aap.inference.routing.management_busy';
        END IF;
        UPDATE public.ml_backend_service_pools AS pool
        SET routing_generation = pool.routing_generation + 1,
            updated_at = pg_catalog.now()
        WHERE pool.id = v_pool_id;
    END LOOP;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_project_pool_before_change()
    FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_route_registry_after_insert
AFTER INSERT ON public.ml_backend_registry
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._routing_registry_after_insert();
-- aap-statement-break
CREATE TRIGGER aap_route_registry_before_change
BEFORE UPDATE OR DELETE ON public.ml_backend_registry
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._routing_registry_before_change();
-- aap-statement-break
CREATE TRIGGER aap_route_pool_before_change
BEFORE UPDATE OR DELETE ON public.ml_backend_service_pools
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._routing_pool_before_change();
-- aap-statement-break
CREATE TRIGGER aap_route_pool_after_generation
AFTER UPDATE OF routing_generation ON public.ml_backend_service_pools
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._routing_pool_after_generation();
-- aap-statement-break
CREATE TRIGGER aap_route_member_before_change
BEFORE INSERT OR UPDATE OR DELETE ON public.ml_backend_pool_members
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._routing_member_before_change();
-- aap-statement-break
CREATE TRIGGER aap_route_project_pool_before_change
BEFORE INSERT OR UPDATE OR DELETE ON public.project_ml_backend_pool
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._routing_project_pool_before_change();
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1.routing_policy()
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
        'schema', 'aap-inference-routing-native/v1',
        'data_protocol', 'aap-inference-routing-data/v1',
        'transaction', 'caller_owned',
        'functions', pg_catalog.jsonb_build_array(
            'aap_inference_routing_v1.routing_policy()',
            'aap_inference_routing_v1.routing_step(text,text)'
        ),
        'actions', pg_catalog.jsonb_build_array(
            'command_status', 'record_command', 'admission_snapshot', 'reserve_scope',
            'scope_status', 'begin_attempt', 'settle_attempt', 'heartbeat_scope',
            'close_scope', 'quiescence', 'prepare_control', 'finish_control',
            'prepare_epoch', 'finish_epoch', 'capture_transport_subject',
            'open_transport_subject', 'begin_control_attempt', 'settle_control_attempt',
            'runtime_snapshot'
        ),
        'ready', aap_inference_routing_v1._routing_secret() IS NOT NULL
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.routing_policy() FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1.routing_step(
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
    v_id uuid;
    v_request_id uuid;
    v_scope_id uuid;
    v_control_id uuid;
    v_attempt_id uuid;
    v_registry_id uuid;
    v_pool_id uuid;
    v_project_id uuid;
    v_required_instance_id uuid;
    v_physical_pool_id uuid;
    v_member_id uuid;
    v_generation bigint;
    v_config_revision bigint;
    v_eligibility_revision bigint;
    v_authority_epoch uuid;
    v_realm text;
    v_mode text;
    v_operation text;
    v_owner text;
    v_sha text;
    v_capability text;
    v_snapshot_token text;
    v_snapshot jsonb;
    v_payload jsonb;
    v_result jsonb;
    v_scope_view jsonb;
    v_control_view jsonb;
    v_members jsonb := '[]'::jsonb;
    v_snapshot_members jsonb := '[]'::jsonb;
    v_pool_row jsonb;
    v_registry_row jsonb;
    v_member_row jsonb;
    v_initial_members jsonb := '[]'::jsonb;
    v_current_members jsonb := '[]'::jsonb;
    v_registry_ids uuid[] := ARRAY[]::uuid[];
    v_candidate_ids uuid[] := ARRAY[]::uuid[];
    v_eligible_ids uuid[] := ARRAY[]::uuid[];
    v_eligible_weights integer[] := ARRAY[]::integer[];
    v_candidate text;
    v_json_element jsonb;
    v_now timestamptz;
    v_deadline timestamptz;
    v_ttl bigint;
    v_health_max_age bigint;
    v_association_enabled boolean;
    v_pool_enabled boolean;
    v_legacy_instance uuid;
    v_current_count bigint;
    v_max_concurrency integer;
    v_weight integer;
    v_total_weight bigint := 0;
    v_current_weight bigint;
    v_winner uuid;
    v_winner_weight bigint;
    v_found_candidate boolean := false;
    v_state record;
    v_scope record;
    v_attempt record;
    v_control record;
    v_receipt record;
    v_registry record;
    v_pool record;
    v_member record;
    v_transport jsonb;
    v_transport_hash text;
    v_header_token text;
    v_new_epoch uuid;
    v_retired_epoch uuid;
    v_challenge text;
    v_epoch_key bytea;
    v_reply jsonb;
    v_reply_incarnation uuid;
    v_reply_command uuid;
    v_reply_registry uuid;
    v_reply_epoch uuid;
    v_attempt_count bigint;
    v_active_count bigint;
    v_uncertain_count bigint;
    v_control_count bigint;
    v_response_count bigint;
    v_failed_response boolean;
    v_topology jsonb;
    v_expected_topology jsonb;
    v_locked_pool_id uuid;
    v_item uuid;
    v_deadline_ms bigint;
    v_creating_xid xid8;
    v_secret bytea;
    v_origin_xid xid8;
    v_subject text;
    v_plaintext text;
    v_ciphertext bytea;
    v_subject_json jsonb;
    v_identity_hash text;
    v_method text;
    v_path text;
    v_reason text;
    v_status_code integer;
    v_finish_kind text;
    v_index integer;
BEGIN
    IF p_action IS NULL OR p_arguments IS NULL
       OR pg_catalog.octet_length(p_action) > 48
       OR pg_catalog.octet_length(p_arguments) > 16777216 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing request';
    END IF;
    BEGIN
        v_args := p_arguments::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing request';
    END;
    IF pg_catalog.jsonb_typeof(v_args) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing request';
    END IF;

    CASE p_action
    WHEN 'command_status' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['request_id', 'command_sha256']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing command status';
        END IF;
        v_request_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'request_id');
        v_sha := v_args ->> 'command_sha256';
        IF pg_catalog.jsonb_typeof(v_args -> 'command_sha256') IS DISTINCT FROM 'string'
           OR v_sha !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing command status';
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-routing-command:' || v_request_id::text, 412091)
        );
        SELECT receipt.id, receipt.command_sha256, receipt.operation,
               receipt.result, receipt.creating_xid, receipt.realm
        INTO v_receipt
        FROM aap_inference_routing_v1.command_receipts AS receipt
        WHERE receipt.id = v_request_id;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._routing_reply(
                'ok', pg_catalog.jsonb_build_object('receipt', NULL)
            );
        END IF;
        IF v_receipt.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
           OR aap_inference_routing_v1._routing_secret() IS NULL THEN
            RETURN aap_inference_routing_v1._routing_reply(
                'ok', pg_catalog.jsonb_build_object('receipt', NULL)
            );
        END IF;
        IF v_receipt.command_sha256 IS DISTINCT FROM v_sha THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing command identity conflict';
        END IF;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'receipt', pg_catalog.jsonb_build_object(
                'request_id', v_request_id,
                'command_sha256', v_receipt.command_sha256,
                'operation', v_receipt.operation,
                'state', CASE WHEN v_receipt.creating_xid = pg_catalog.pg_current_xact_id()
                              THEN 'caller_pending' ELSE 'committed' END,
                'result', v_receipt.result
            )
        ));

    WHEN 'record_command' THEN
        IF aap_inference_routing_v1._routing_secret() IS NULL THEN
            RETURN aap_inference_routing_v1._routing_rejected('backend_not_ready');
        END IF;
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['request_id', 'command_sha256', 'operation', 'result']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing command receipt';
        END IF;
        v_request_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'request_id');
        v_sha := v_args ->> 'command_sha256';
        v_operation := v_args ->> 'operation';
        IF pg_catalog.jsonb_typeof(v_args -> 'command_sha256') IS DISTINCT FROM 'string'
           OR v_sha !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(v_args -> 'operation') IS DISTINCT FROM 'string'
           OR v_operation NOT IN ('acquire', 'begin', 'settle', 'heartbeat', 'close',
                                  'scope_status', 'reconcile', 'prepare_control', 'finish_control',
                                  'begin_control', 'settle_control')
           OR pg_catalog.jsonb_typeof(v_args -> 'result') IS DISTINCT FROM 'object'
           OR pg_catalog.octet_length((v_args -> 'result')::text) > 65536
           OR NOT aap_inference_routing_v1._routing_receipt_safe(v_args -> 'result') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing command receipt';
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-routing-command:' || v_request_id::text, 412091)
        );
        SELECT receipt.id, receipt.command_sha256, receipt.operation,
               receipt.result, receipt.creating_xid, receipt.realm
        INTO v_receipt
        FROM aap_inference_routing_v1.command_receipts AS receipt
        WHERE receipt.id = v_request_id
        FOR UPDATE;
        IF FOUND THEN
            IF v_receipt.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
               OR v_receipt.command_sha256 IS DISTINCT FROM v_sha
               OR v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.result IS DISTINCT FROM (v_args -> 'result') THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing command identity conflict';
            END IF;
        ELSE
            INSERT INTO aap_inference_routing_v1.command_receipts (
                id, command_sha256, operation, result, creating_xid, realm
            ) VALUES (
                v_request_id, v_sha, v_operation, v_args -> 'result',
                pg_catalog.pg_current_xact_id(), aap_inference_routing_v1._routing_realm()
            );
            SELECT v_request_id AS id, v_sha AS command_sha256,
                   v_operation AS operation, (v_args -> 'result') AS result,
                   pg_catalog.pg_current_xact_id() AS creating_xid
            INTO v_receipt;
        END IF;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'receipt', pg_catalog.jsonb_build_object(
                'request_id', v_request_id,
                'command_sha256', v_sha,
                'operation', v_operation,
                'state', CASE WHEN v_receipt.creating_xid = pg_catalog.pg_current_xact_id()
                              THEN 'caller_pending' ELSE 'committed' END,
                'result', v_receipt.result
            )
        ));

    WHEN 'admission_snapshot' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['pool_id', 'registry_id', 'project_id', 'mode',
                          'required_instance_id', 'health_max_age_seconds']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing snapshot request';
        END IF;
        v_mode := v_args ->> 'mode';
        IF pg_catalog.jsonb_typeof(v_args -> 'mode') IS DISTINCT FROM 'string'
           OR v_mode NOT IN ('off', 'observe', 'enforce', 'direct') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing snapshot request';
        END IF;
        v_health_max_age := aap_inference_routing_v1._routing_integer(
            v_args -> 'health_max_age_seconds', 1, 604800
        );
        IF v_args -> 'pool_id' <> 'null'::jsonb THEN
            v_pool_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'pool_id');
        ELSE
            v_pool_id := NULL;
        END IF;
        IF v_args -> 'registry_id' <> 'null'::jsonb THEN
            v_registry_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'registry_id');
        ELSE
            v_registry_id := NULL;
        END IF;
        IF v_args -> 'project_id' <> 'null'::jsonb THEN
            v_project_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'project_id');
        ELSE
            v_project_id := NULL;
        END IF;
        IF v_args -> 'required_instance_id' <> 'null'::jsonb THEN
            v_required_instance_id := aap_inference_routing_v1._routing_uuid(
                v_args ->> 'required_instance_id'
            );
        ELSE
            v_required_instance_id := NULL;
        END IF;
        IF (v_mode = 'direct' AND (v_pool_id IS NOT NULL OR v_registry_id IS NULL))
           OR (v_mode <> 'direct' AND (v_pool_id IS NULL OR v_registry_id IS NOT NULL))
           OR (v_mode = 'direct' AND v_required_instance_id IS NOT NULL
               AND v_required_instance_id IS DISTINCT FROM v_registry_id) THEN
            RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
        END IF;
        v_realm := aap_inference_routing_v1._routing_realm();
        IF v_realm IS NULL OR aap_inference_routing_v1._routing_secret() IS NULL THEN
            RETURN aap_inference_routing_v1._routing_rejected('backend_not_ready');
        END IF;

        IF v_mode = 'direct' THEN
            v_registry_ids := ARRAY[v_registry_id];
            SELECT member.pool_id
            INTO v_physical_pool_id
            FROM public.ml_backend_pool_members AS member
            WHERE member.registry_id = v_registry_id;
            v_physical_pool_id := CASE WHEN FOUND THEN v_physical_pool_id ELSE NULL END;
        ELSE
            SELECT pg_catalog.to_jsonb(pool), pool.enabled, pool.legacy_instance_id,
                   pool.routing_generation
            INTO v_pool_row, v_pool_enabled, v_legacy_instance, v_generation
            FROM public.ml_backend_service_pools AS pool
            WHERE pool.id = v_pool_id;
            IF NOT FOUND THEN
                RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
            END IF;
            IF v_pool_enabled IS DISTINCT FROM TRUE OR v_legacy_instance IS NULL THEN
                RETURN aap_inference_routing_v1._routing_rejected('pool_not_enabled');
            END IF;
            v_physical_pool_id := v_pool_id;
            SELECT COALESCE(pg_catalog.array_agg(member.registry_id ORDER BY member.registry_id),
                            ARRAY[]::uuid[]),
                   COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(member)
                             ORDER BY member.registry_id), '[]'::jsonb)
            INTO v_registry_ids, v_initial_members
            FROM public.ml_backend_pool_members AS member
            WHERE member.pool_id = v_pool_id;
        END IF;

        BEGIN
            FOR v_item IN
                SELECT item.registry_id
                FROM pg_catalog.unnest(v_registry_ids) AS item(registry_id)
                ORDER BY item.registry_id
            LOOP
                PERFORM registry.id
                FROM public.ml_backend_registry AS registry
                WHERE registry.id = v_item
                FOR SHARE NOWAIT;
                IF NOT FOUND THEN
                    RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
                END IF;
            END LOOP;
            IF v_physical_pool_id IS NOT NULL THEN
                PERFORM aap_inference_routing_v1._routing_lock_admission_pool(v_physical_pool_id);
                SELECT pg_catalog.to_jsonb(pool), pool.enabled, pool.legacy_instance_id,
                       pool.routing_generation
                INTO v_pool_row, v_pool_enabled, v_legacy_instance, v_generation
                FROM public.ml_backend_service_pools AS pool
                WHERE pool.id = v_physical_pool_id;
                IF NOT FOUND THEN
                    RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
                END IF;
            END IF;
            IF v_mode = 'direct' AND v_physical_pool_id IS NOT NULL THEN
                PERFORM member.id
                FROM public.ml_backend_pool_members AS member
                WHERE member.pool_id = v_physical_pool_id
                  AND member.registry_id = v_registry_id
                FOR SHARE NOWAIT;
            ELSIF v_mode <> 'direct' THEN
                PERFORM member.id
                FROM public.ml_backend_pool_members AS member
                WHERE member.pool_id = v_pool_id
                ORDER BY member.registry_id
                FOR SHARE NOWAIT;
            END IF;
        EXCEPTION WHEN lock_not_available THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END;

        IF v_mode <> 'direct' THEN
            SELECT COALESCE(pg_catalog.array_agg(member.registry_id ORDER BY member.registry_id),
                            ARRAY[]::uuid[]),
                   COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(member)
                             ORDER BY member.registry_id), '[]'::jsonb)
            INTO v_candidate_ids, v_current_members
            FROM public.ml_backend_pool_members AS member
            WHERE member.pool_id = v_pool_id;
            IF v_candidate_ids IS DISTINCT FROM v_registry_ids
               OR v_current_members IS DISTINCT FROM v_initial_members THEN
                RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
            END IF;
            IF v_pool_enabled IS DISTINCT FROM TRUE OR v_legacy_instance IS NULL THEN
                RETURN aap_inference_routing_v1._routing_rejected('pool_not_enabled');
            END IF;
            IF v_project_id IS NOT NULL THEN
                SELECT association.enabled
                INTO v_association_enabled
                FROM public.project_ml_backend_pool AS association
                WHERE association.project_id = v_project_id
                  AND association.pool_id = v_pool_id;
                IF NOT FOUND OR v_association_enabled IS DISTINCT FROM TRUE THEN
                    RETURN aap_inference_routing_v1._routing_rejected('pool_not_enabled');
                END IF;
            END IF;
        ELSE
            IF v_physical_pool_id IS NOT NULL THEN
                SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(member)
                             ORDER BY member.registry_id), '[]'::jsonb)
                INTO v_current_members
                FROM public.ml_backend_pool_members AS member
                WHERE member.pool_id = v_physical_pool_id
                  AND member.registry_id = v_registry_id;
            ELSE
                v_current_members := '[]'::jsonb;
            END IF;
        END IF;

        FOR v_registry_id IN
            SELECT item.registry_id
            FROM pg_catalog.unnest(v_registry_ids) AS item(registry_id)
            ORDER BY item.registry_id
        LOOP
            SELECT registry.*
            INTO v_registry
            FROM public.ml_backend_registry AS registry
            WHERE registry.id = v_registry_id;
            IF NOT FOUND THEN
                RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
            END IF;
            SELECT state.*
            INTO v_state
            FROM aap_inference_routing_v1.registry_state AS state
            WHERE state.registry_id = v_registry_id;
            IF NOT FOUND OR v_state.realm IS DISTINCT FROM v_realm THEN
                RETURN aap_inference_routing_v1._routing_rejected('backend_not_ready');
            END IF;
            IF v_mode = 'direct' THEN
                SELECT pg_catalog.to_jsonb(member)
                INTO v_member_row
                FROM public.ml_backend_pool_members AS member
                WHERE member.registry_id = v_registry_id;
            ELSE
                SELECT pg_catalog.to_jsonb(member)
                INTO v_member_row
                FROM public.ml_backend_pool_members AS member
                WHERE member.pool_id = v_pool_id
                  AND member.registry_id = v_registry_id;
            END IF;
            IF NOT FOUND THEN
                v_member_row := NULL;
            END IF;
            v_max_concurrency := aap_inference_routing_v1._routing_max_concurrency(v_registry_id);
            v_members := v_members || pg_catalog.jsonb_build_array(
                pg_catalog.jsonb_build_object(
                    'member', v_member_row,
                    'registry', pg_catalog.to_jsonb(v_registry),
                    'max_concurrency', v_max_concurrency
                )
            );
            v_snapshot_members := v_snapshot_members || pg_catalog.jsonb_build_array(
                pg_catalog.jsonb_build_object(
                    'registry_id', v_registry_id,
                    'member_id', CASE WHEN v_member_row IS NULL THEN NULL ELSE (v_member_row ->> 'id')::uuid END,
                    'pool_id', CASE WHEN v_member_row IS NULL THEN NULL ELSE (v_member_row ->> 'pool_id')::uuid END,
                    'traffic_state', v_member_row ->> 'traffic_state',
                    'weight', CASE WHEN v_member_row IS NULL THEN NULL ELSE (v_member_row ->> 'weight')::integer END,
                    'config_revision', v_state.config_revision,
                    'eligibility_revision', v_state.eligibility_revision,
                    'authority_epoch', v_state.authority_epoch,
                    'max_concurrency', v_max_concurrency
                )
            );
        END LOOP;

        v_payload := pg_catalog.jsonb_build_object(
            'schema', 'aap-inference-routing-snapshot/v1',
            'realm', v_realm,
            'xid', pg_catalog.pg_current_xact_id()::text,
            'mode', v_mode,
            'pool_id', v_pool_id,
            'physical_pool_id', v_physical_pool_id,
            'physical_generation', CASE WHEN v_mode = 'direct' THEN v_generation ELSE NULL END,
            'registry_id', CASE WHEN v_mode = 'direct' THEN v_registry_id ELSE NULL END,
            'project_id', v_project_id,
            'required_instance_id', v_required_instance_id,
            'generation', v_generation,
            'health_max_age_seconds', v_health_max_age,
            'pool_enabled', v_pool_enabled,
            'legacy_instance_id', v_legacy_instance,
            'members', v_snapshot_members
        );
        v_snapshot_token := aap_inference_routing_v1._routing_sign_snapshot(v_payload);
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'snapshot', v_snapshot_token,
            'pool', CASE WHEN v_mode = 'direct' THEN NULL ELSE v_pool_row END,
            'members', v_members,
            'observed_at', pg_catalog.clock_timestamp()
        ));

    WHEN 'reserve_scope' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['snapshot', 'candidate_ids', 'scope_id', 'command_sha256',
                          'owner', 'operation', 'ttl_ms']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing scope request';
        END IF;
        v_snapshot_token := v_args ->> 'snapshot';
        IF pg_catalog.jsonb_typeof(v_args -> 'snapshot') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_args -> 'candidate_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(v_args -> 'command_sha256') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_args -> 'owner') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_args -> 'operation') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing scope request';
        END IF;
        v_scope_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'scope_id');
        v_sha := v_args ->> 'command_sha256';
        v_owner := v_args ->> 'owner';
        v_operation := v_args ->> 'operation';
        v_ttl := aap_inference_routing_v1._routing_integer(v_args -> 'ttl_ms', 1, 2147483647);
        IF v_sha !~ '^[0-9a-f]{64}$'
           OR pg_catalog.char_length(v_owner) NOT BETWEEN 1 AND 512
           OR pg_catalog.char_length(v_operation) NOT BETWEEN 1 AND 96 THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing scope request';
        END IF;
        IF v_operation LIKE 'control:%' THEN
            RETURN aap_inference_routing_v1._routing_rejected('invalid_command');
        END IF;
        v_snapshot := aap_inference_routing_v1._routing_open_snapshot(v_snapshot_token);
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_snapshot, ARRAY['schema', 'realm', 'xid', 'mode', 'pool_id',
                              'physical_pool_id', 'physical_generation', 'registry_id',
                              'project_id', 'required_instance_id', 'generation', 'health_max_age_seconds',
                              'pool_enabled', 'legacy_instance_id', 'members']
        ) OR v_snapshot ->> 'schema' IS DISTINCT FROM 'aap-inference-routing-snapshot/v1'
          OR v_snapshot ->> 'realm' IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
          OR v_snapshot ->> 'xid' IS DISTINCT FROM pg_catalog.pg_current_xact_id()::text
          OR pg_catalog.jsonb_typeof(v_snapshot -> 'members') IS DISTINCT FROM 'array' THEN
            RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
        END IF;
        v_mode := v_snapshot ->> 'mode';
        v_realm := v_snapshot ->> 'realm';
        IF v_snapshot -> 'pool_id' <> 'null'::jsonb THEN
            v_pool_id := aap_inference_routing_v1._routing_uuid(v_snapshot ->> 'pool_id');
        ELSE
            v_pool_id := NULL;
        END IF;
        IF v_snapshot -> 'registry_id' <> 'null'::jsonb THEN
            v_registry_id := aap_inference_routing_v1._routing_uuid(v_snapshot ->> 'registry_id');
        ELSE
            v_registry_id := NULL;
        END IF;
        IF v_snapshot -> 'project_id' <> 'null'::jsonb THEN
            v_project_id := aap_inference_routing_v1._routing_uuid(v_snapshot ->> 'project_id');
        ELSE
            v_project_id := NULL;
        END IF;
        IF v_snapshot -> 'required_instance_id' <> 'null'::jsonb THEN
            v_required_instance_id := aap_inference_routing_v1._routing_uuid(
                v_snapshot ->> 'required_instance_id'
            );
        ELSE
            v_required_instance_id := NULL;
        END IF;
        IF v_snapshot -> 'physical_pool_id' <> 'null'::jsonb THEN
            v_physical_pool_id := aap_inference_routing_v1._routing_uuid(
                v_snapshot ->> 'physical_pool_id'
            );
        ELSE
            v_physical_pool_id := NULL;
        END IF;
        IF v_mode = 'direct' AND v_registry_id IS NULL THEN
            RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
        ELSIF v_mode <> 'direct' AND v_pool_id IS NULL THEN
            RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
        END IF;

        SELECT scope.id, scope.command_sha256, scope.owner, scope.operation,
               scope.registry_id, scope.pool_id, scope.project_id, scope.mode
        INTO v_scope
        FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.id = v_scope_id;
        IF FOUND THEN
            IF v_scope.command_sha256 IS DISTINCT FROM v_sha
               OR v_scope.owner IS DISTINCT FROM v_owner
               OR v_scope.operation IS DISTINCT FROM v_operation
               OR v_scope.mode IS DISTINCT FROM v_mode
               OR v_scope.pool_id IS DISTINCT FROM v_pool_id
               OR v_scope.project_id IS DISTINCT FROM v_project_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing scope identity conflict';
            END IF;
            RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
                'scope', aap_inference_routing_v1._routing_scope_view(v_scope_id, true)
            ));
        END IF;

        IF pg_catalog.jsonb_array_length(v_args -> 'candidate_ids') = 0 THEN
            RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
        END IF;
        FOR v_json_element IN
            SELECT element.value
            FROM pg_catalog.jsonb_array_elements(v_args -> 'candidate_ids') AS element(value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_json_element) IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing candidate';
            END IF;
            v_item := aap_inference_routing_v1._routing_uuid(v_json_element #>> '{}');
            IF v_item = ANY(v_candidate_ids) THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'duplicate routing candidate';
            END IF;
            v_candidate_ids := pg_catalog.array_append(v_candidate_ids, v_item);
        END LOOP;
        IF v_mode = 'direct' AND (
            pg_catalog.cardinality(v_candidate_ids) <> 1
            OR v_candidate_ids[1] IS DISTINCT FROM v_registry_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid direct routing candidate';
        END IF;
        IF v_required_instance_id IS NOT NULL AND (
            pg_catalog.cardinality(v_candidate_ids) <> 1
            OR v_candidate_ids[1] IS DISTINCT FROM v_required_instance_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing candidate violates required instance';
        END IF;

        v_registry_ids := ARRAY[]::uuid[];
        FOR v_json_element IN
            SELECT element.value
            FROM pg_catalog.jsonb_array_elements(v_snapshot -> 'members') AS element(value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_json_element) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_json_element -> 'registry_id') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing snapshot';
            END IF;
            v_item := aap_inference_routing_v1._routing_uuid(v_json_element ->> 'registry_id');
            IF v_item = ANY(v_registry_ids) THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing snapshot';
            END IF;
            v_registry_ids := pg_catalog.array_append(v_registry_ids, v_item);
        END LOOP;
        IF v_mode = 'direct' AND NOT (v_registry_id = ANY(v_registry_ids)) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing snapshot';
        END IF;
        FOREACH v_item IN ARRAY v_candidate_ids LOOP
            IF NOT (v_item = ANY(v_registry_ids)) THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing candidate outside snapshot';
            END IF;
        END LOOP;

        BEGIN
            FOR v_item IN
                SELECT item.registry_id
                FROM pg_catalog.unnest(v_registry_ids) AS item(registry_id)
                ORDER BY item.registry_id
            LOOP
                PERFORM registry.id
                FROM public.ml_backend_registry AS registry
                WHERE registry.id = v_item
                FOR SHARE NOWAIT;
                IF NOT FOUND THEN
                    RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
                END IF;
            END LOOP;
            IF v_physical_pool_id IS NOT NULL THEN
                PERFORM aap_inference_routing_v1._routing_lock_admission_pool(v_physical_pool_id);
                SELECT pool.routing_generation, pool.enabled, pool.legacy_instance_id
                INTO v_generation, v_pool_enabled, v_legacy_instance
                FROM public.ml_backend_service_pools AS pool
                WHERE pool.id = v_physical_pool_id;
                IF NOT FOUND THEN
                    RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
                END IF;
                PERFORM member.id
                FROM public.ml_backend_pool_members AS member
                WHERE member.pool_id = v_physical_pool_id
                ORDER BY member.registry_id
                FOR SHARE NOWAIT;
            END IF;
        EXCEPTION WHEN lock_not_available THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END;

        IF v_mode <> 'direct' THEN
            IF v_pool_enabled IS DISTINCT FROM TRUE OR v_legacy_instance IS NULL
               OR v_generation IS DISTINCT FROM (v_snapshot ->> 'generation')::bigint THEN
                RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
            END IF;
            IF v_project_id IS NOT NULL THEN
                SELECT association.enabled
                INTO v_association_enabled
                FROM public.project_ml_backend_pool AS association
                WHERE association.project_id = v_project_id
                  AND association.pool_id = v_pool_id;
                IF NOT FOUND OR v_association_enabled IS DISTINCT FROM TRUE THEN
                    RETURN aap_inference_routing_v1._routing_rejected('pool_not_enabled');
                END IF;
            END IF;
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                         'id', member.id,
                         'pool_id', member.pool_id,
                         'registry_id', member.registry_id,
                         'traffic_state', member.traffic_state,
                         'weight', member.weight
                     ) ORDER BY member.registry_id), '[]'::jsonb)
            INTO v_current_members
            FROM public.ml_backend_pool_members AS member
            WHERE member.pool_id = v_pool_id;
            v_initial_members := (
                SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                    'id', element.value -> 'member_id',
                    'pool_id', element.value -> 'pool_id',
                    'registry_id', element.value -> 'registry_id',
                    'traffic_state', element.value -> 'traffic_state',
                    'weight', element.value -> 'weight'
                ) ORDER BY element.value ->> 'registry_id'), '[]'::jsonb)
                FROM pg_catalog.jsonb_array_elements(v_snapshot -> 'members') AS element(value)
            );
            IF v_current_members IS DISTINCT FROM v_initial_members THEN
                RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
            END IF;
        ELSIF v_physical_pool_id IS NOT NULL THEN
            IF v_generation IS DISTINCT FROM (v_snapshot ->> 'physical_generation')::bigint THEN
                RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
            END IF;
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                         'id', member.id,
                         'pool_id', member.pool_id,
                         'registry_id', member.registry_id,
                         'traffic_state', member.traffic_state,
                         'weight', member.weight
                     ) ORDER BY member.registry_id), '[]'::jsonb)
            INTO v_current_members
            FROM public.ml_backend_pool_members AS member
            WHERE member.pool_id = v_physical_pool_id
              AND member.registry_id = v_registry_id;
            v_initial_members := (
                SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                    'id', element.value -> 'member_id',
                    'pool_id', element.value -> 'pool_id',
                    'registry_id', element.value -> 'registry_id',
                    'traffic_state', element.value -> 'traffic_state',
                    'weight', element.value -> 'weight'
                ) ORDER BY element.value ->> 'registry_id'), '[]'::jsonb)
                FROM pg_catalog.jsonb_array_elements(v_snapshot -> 'members') AS element(value)
                WHERE element.value ->> 'registry_id' = v_registry_id::text
            );
            IF v_current_members IS DISTINCT FROM v_initial_members THEN
                RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
            END IF;
        END IF;

        FOR v_json_element IN
            SELECT element.value
            FROM pg_catalog.jsonb_array_elements(v_snapshot -> 'members') AS element(value)
        LOOP
            v_item := aap_inference_routing_v1._routing_uuid(v_json_element ->> 'registry_id');
            SELECT state.config_revision, state.eligibility_revision, state.authority_epoch
            INTO v_state
            FROM aap_inference_routing_v1.registry_state AS state
            WHERE state.registry_id = v_item
              AND state.realm = v_realm;
            IF NOT FOUND
               OR v_state.config_revision IS DISTINCT FROM (v_json_element ->> 'config_revision')::bigint
               OR v_state.eligibility_revision IS DISTINCT FROM (v_json_element ->> 'eligibility_revision')::bigint
               OR v_state.authority_epoch IS DISTINCT FROM (v_json_element ->> 'authority_epoch')::uuid THEN
                RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
            END IF;
        END LOOP;

        v_now := pg_catalog.clock_timestamp();
        v_deadline := v_now + v_ttl * interval '1 millisecond';
        IF v_mode = 'direct' THEN
            v_registry_id := aap_inference_routing_v1._routing_uuid(v_snapshot ->> 'registry_id');
            IF aap_inference_routing_v1._routing_has_control(v_registry_id) THEN
                RETURN aap_inference_routing_v1._routing_rejected('management_busy');
            END IF;
            SELECT state.config_revision, state.eligibility_revision, state.authority_epoch
            INTO v_state
            FROM aap_inference_routing_v1.registry_state AS state
            WHERE state.registry_id = v_registry_id
              AND state.realm = v_realm;
            IF NOT FOUND THEN
                RETURN aap_inference_routing_v1._routing_rejected('backend_not_ready');
            END IF;
            INSERT INTO aap_inference_routing_v1.scopes (
                id, command_sha256, registry_id, pool_id, project_id, mode,
                owner, operation, generation, config_revision, eligibility_revision,
                authority_epoch, realm, state, closed, heartbeat_deadline
            ) VALUES (
                v_scope_id, v_sha, v_registry_id, NULL, v_project_id, v_mode,
                v_owner, v_operation, NULL, v_state.config_revision,
                v_state.eligibility_revision, v_state.authority_epoch, v_realm,
                'reserved', false, v_deadline
            );
        ELSIF v_mode IN ('off', 'observe') THEN
            IF pg_catalog.cardinality(v_candidate_ids) <> 1
               OR v_candidate_ids[1] IS DISTINCT FROM v_legacy_instance THEN
                RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
            END IF;
            v_winner := v_candidate_ids[1];
            IF aap_inference_routing_v1._routing_has_control(v_winner) THEN
                RETURN aap_inference_routing_v1._routing_rejected('management_busy');
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.ml_backend_pool_members AS member
                WHERE member.pool_id = v_pool_id
                  AND member.registry_id = v_winner
            ) THEN
                RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
            END IF;
            SELECT state.config_revision, state.eligibility_revision, state.authority_epoch
            INTO v_state
            FROM aap_inference_routing_v1.registry_state AS state
            WHERE state.registry_id = v_winner
              AND state.realm = v_realm;
            IF NOT FOUND THEN
                RETURN aap_inference_routing_v1._routing_rejected('backend_not_ready');
            END IF;
            INSERT INTO aap_inference_routing_v1.scopes (
                id, command_sha256, registry_id, pool_id, project_id, mode,
                owner, operation, generation, config_revision, eligibility_revision,
                authority_epoch, realm, state, closed, heartbeat_deadline
            ) VALUES (
                v_scope_id, v_sha, v_winner, v_pool_id, v_project_id, v_mode,
                v_owner, v_operation, v_generation, v_state.config_revision,
                v_state.eligibility_revision, v_state.authority_epoch, v_realm,
                'reserved', false, v_deadline
            );
        ELSE
            v_eligible_ids := ARRAY[]::uuid[];
            v_eligible_weights := ARRAY[]::integer[];
            v_control_count := 0;
            FOREACH v_item IN ARRAY v_candidate_ids LOOP
                IF aap_inference_routing_v1._routing_has_control(v_item) THEN
                    v_control_count := v_control_count + 1;
                    CONTINUE;
                END IF;
                SELECT member.weight
                INTO v_weight
                FROM public.ml_backend_pool_members AS member
                WHERE member.pool_id = v_pool_id
                  AND member.registry_id = v_item
                  AND member.traffic_state = 'active';
                IF NOT FOUND THEN
                    CONTINUE;
                END IF;
                v_max_concurrency := aap_inference_routing_v1._routing_max_concurrency(v_item);
                SELECT pg_catalog.count(*)
                INTO v_current_count
                FROM aap_inference_routing_v1.scopes AS scope
                WHERE scope.registry_id = v_item
                  AND (scope.state IN ('active', 'uncertain')
                       OR (scope.state = 'reserved' AND scope.heartbeat_deadline > v_now));
                IF v_current_count < v_max_concurrency THEN
                    v_eligible_ids := pg_catalog.array_append(v_eligible_ids, v_item);
                    v_eligible_weights := pg_catalog.array_append(v_eligible_weights, v_weight);
                END IF;
            END LOOP;
            IF pg_catalog.cardinality(v_eligible_ids) = 0 THEN
                IF v_control_count > 0 THEN
                    RETURN aap_inference_routing_v1._routing_rejected('management_busy');
                END IF;
                IF EXISTS (
                    SELECT 1 FROM public.ml_backend_pool_members AS member
                    WHERE member.pool_id = v_pool_id
                      AND member.registry_id = ANY(v_candidate_ids)
                      AND member.traffic_state = 'active'
                ) THEN
                    RETURN aap_inference_routing_v1._routing_rejected('pool_saturated');
                END IF;
                RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
            END IF;
            v_total_weight := 0;
            v_winner := NULL;
            v_winner_weight := NULL;
            FOR v_index IN 1..pg_catalog.cardinality(v_eligible_ids) LOOP
                INSERT INTO aap_inference_routing_v1.weights (
                    pool_id, registry_id, current_weight
                ) VALUES (v_pool_id, v_eligible_ids[v_index], 0)
                ON CONFLICT (pool_id, registry_id) DO NOTHING;
                UPDATE aap_inference_routing_v1.weights AS weight
                SET current_weight = weight.current_weight + v_eligible_weights[v_index]
                WHERE weight.pool_id = v_pool_id
                  AND weight.registry_id = v_eligible_ids[v_index]
                RETURNING weight.current_weight INTO v_current_weight;
                v_total_weight := v_total_weight + v_eligible_weights[v_index];
                IF v_winner IS NULL OR v_current_weight > v_winner_weight
                   OR (v_current_weight = v_winner_weight
                       AND v_eligible_ids[v_index] < v_winner) THEN
                    v_winner := v_eligible_ids[v_index];
                    v_winner_weight := v_current_weight;
                END IF;
            END LOOP;
            UPDATE aap_inference_routing_v1.weights AS weight
            SET current_weight = weight.current_weight - v_total_weight
            WHERE weight.pool_id = v_pool_id
              AND weight.registry_id = v_winner;
            SELECT state.config_revision, state.eligibility_revision, state.authority_epoch
            INTO v_state
            FROM aap_inference_routing_v1.registry_state AS state
            WHERE state.registry_id = v_winner
              AND state.realm = v_realm;
            IF NOT FOUND THEN
                RETURN aap_inference_routing_v1._routing_rejected('backend_not_ready');
            END IF;
            INSERT INTO aap_inference_routing_v1.scopes (
                id, command_sha256, registry_id, pool_id, project_id, mode,
                owner, operation, generation, config_revision, eligibility_revision,
                authority_epoch, realm, state, closed, heartbeat_deadline
            ) VALUES (
                v_scope_id, v_sha, v_winner, v_pool_id, v_project_id, v_mode,
                v_owner, v_operation, v_generation, v_state.config_revision,
                v_state.eligibility_revision, v_state.authority_epoch, v_realm,
                'reserved', false, v_deadline
            );
        END IF;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'scope', aap_inference_routing_v1._routing_scope_view(v_scope_id, true)
        ));

    WHEN 'scope_status' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['scope_id', 'capability']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing scope status';
        END IF;
        v_scope_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'scope_id');
        v_capability := v_args ->> 'capability';
        IF pg_catalog.jsonb_typeof(v_args -> 'capability') IS DISTINCT FROM 'string'
           OR v_capability !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing scope status';
        END IF;
        SELECT scope.id INTO v_scope FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.id = v_scope_id;
        IF NOT FOUND OR aap_inference_routing_v1._routing_scope_capability(v_scope_id)
            IS DISTINCT FROM v_capability THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
        END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                   'id', attempt.id,
                   'state', attempt.state,
                   'status_code', attempt.status_code,
                   'reason', attempt.reason
               ) ORDER BY attempt.created_at, attempt.id), '[]'::jsonb)
        INTO v_result
        FROM aap_inference_routing_v1.attempts AS attempt
        WHERE attempt.scope_id = v_scope_id;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'scope', aap_inference_routing_v1._routing_scope_view(v_scope_id, true),
            'attempts', v_result
        ));

    WHEN 'begin_attempt' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['scope_id', 'capability', 'attempt_id', 'method', 'path',
                          'transport_identity']
        ) OR NOT aap_inference_routing_v1._routing_exact_keys(
            v_args -> 'transport_identity', ARRAY['identity_sha256']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing attempt';
        END IF;
        v_scope_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'scope_id');
        v_attempt_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'attempt_id');
        v_capability := v_args ->> 'capability';
        v_method := v_args ->> 'method';
        v_path := v_args ->> 'path';
        v_identity_hash := v_args #>> '{transport_identity,identity_sha256}';
        IF pg_catalog.jsonb_typeof(v_args -> 'capability') IS DISTINCT FROM 'string'
           OR v_capability !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(v_args -> 'method') IS DISTINCT FROM 'string'
           OR v_method !~ '^[A-Z]{1,16}$'
           OR pg_catalog.jsonb_typeof(v_args -> 'path') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_path) NOT BETWEEN 1 AND 4096
           OR v_path !~ '^/[^?#[:space:]]*$'
           OR pg_catalog.jsonb_typeof(v_args #> '{transport_identity,identity_sha256}')
                IS DISTINCT FROM 'string'
           OR v_identity_hash !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing attempt';
        END IF;
        SELECT scope.* INTO v_scope
        FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.id = v_scope_id
        FOR UPDATE;
        IF NOT FOUND OR aap_inference_routing_v1._routing_scope_capability(v_scope_id)
            IS DISTINCT FROM v_capability THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
        END IF;
        IF v_scope.closed THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_closed');
        END IF;
        IF v_scope.state = 'reserved' AND v_scope.heartbeat_deadline <= pg_catalog.clock_timestamp() THEN
            IF NOT EXISTS (
                SELECT 1 FROM aap_inference_routing_v1.attempts AS attempt
                WHERE attempt.scope_id = v_scope_id
            ) THEN
                UPDATE aap_inference_routing_v1.scopes AS scope
                SET state = 'expired', closed = true, updated_at = pg_catalog.now()
                WHERE scope.id = v_scope_id;
            END IF;
            RETURN aap_inference_routing_v1._routing_rejected('scope_expired');
        END IF;
        IF v_scope.state NOT IN ('reserved', 'active') THEN
            RETURN aap_inference_routing_v1._routing_rejected(
                CASE WHEN v_scope.state = 'expired' THEN 'scope_expired' ELSE 'scope_unavailable' END
            );
        END IF;
        SELECT attempt.* INTO v_attempt
        FROM aap_inference_routing_v1.attempts AS attempt
        WHERE attempt.id = v_attempt_id;
        IF FOUND THEN
            IF v_attempt.scope_id IS DISTINCT FROM v_scope_id
               OR v_attempt.method IS DISTINCT FROM v_method
               OR v_attempt.path IS DISTINCT FROM v_path
               OR v_attempt.transport_identity_sha256 IS DISTINCT FROM v_identity_hash THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing attempt identity conflict';
            END IF;
            IF v_attempt.state <> 'active' THEN
                RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
            END IF;
        ELSE
            BEGIN
                PERFORM registry.id
                FROM public.ml_backend_registry AS registry
                WHERE registry.id = v_scope.registry_id
                FOR SHARE NOWAIT;
                IF NOT FOUND THEN
                    RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
                END IF;
                IF v_scope.pool_id IS NOT NULL THEN
                    PERFORM aap_inference_routing_v1._routing_lock_admission_pool(v_scope.pool_id);
                    PERFORM pool.id FROM public.ml_backend_service_pools AS pool
                    WHERE pool.id = v_scope.pool_id;
                    IF NOT FOUND THEN
                        RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
                    END IF;
                    PERFORM member.id
                    FROM public.ml_backend_pool_members AS member
                    WHERE member.pool_id = v_scope.pool_id
                      AND member.registry_id = v_scope.registry_id
                    FOR SHARE NOWAIT;
                    IF NOT FOUND THEN
                        RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
                    END IF;
                END IF;
            EXCEPTION WHEN lock_not_available THEN
                RETURN aap_inference_routing_v1._routing_rejected('management_busy');
            END;
            v_transport := aap_inference_routing_v1._routing_transport_identity(v_scope.registry_id);
            IF v_transport IS NULL
               OR pg_catalog.encode(public.digest(
                    pg_catalog.convert_to(v_transport::text, 'UTF8'), 'sha256'
                  ), 'hex') IS DISTINCT FROM v_identity_hash THEN
                RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
            END IF;
            SELECT state.* INTO v_state
            FROM aap_inference_routing_v1.registry_state AS state
            WHERE state.registry_id = v_scope.registry_id;
            IF NOT FOUND OR v_state.realm IS DISTINCT FROM v_scope.realm
               OR v_state.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
               OR v_state.config_revision IS DISTINCT FROM v_scope.config_revision
               OR v_state.authority_epoch IS DISTINCT FROM v_scope.authority_epoch THEN
                RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
            END IF;
            IF v_scope.state = 'reserved' THEN
                IF v_state.eligibility_revision IS DISTINCT FROM v_scope.eligibility_revision THEN
                    RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
                END IF;
                IF v_scope.mode = 'enforce' AND v_state.continuity <> 'ready' THEN
                    RETURN aap_inference_routing_v1._routing_rejected('continuity_unverified');
                END IF;
                IF v_scope.pool_id IS NOT NULL THEN
                    SELECT pool.routing_generation, pool.enabled
                    INTO v_generation, v_pool_enabled
                    FROM public.ml_backend_service_pools AS pool
                    WHERE pool.id = v_scope.pool_id;
                    IF v_generation IS DISTINCT FROM v_scope.generation THEN
                        RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
                    END IF;
                    IF v_pool_enabled IS DISTINCT FROM TRUE THEN
                        RETURN aap_inference_routing_v1._routing_rejected('pool_not_enabled');
                    END IF;
                    SELECT member.traffic_state INTO v_reason
                    FROM public.ml_backend_pool_members AS member
                    WHERE member.pool_id = v_scope.pool_id
                      AND member.registry_id = v_scope.registry_id;
                    IF v_reason IS DISTINCT FROM 'active' THEN
                        RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
                    END IF;
                    IF v_scope.project_id IS NOT NULL THEN
                        SELECT association.enabled INTO v_association_enabled
                        FROM public.project_ml_backend_pool AS association
                        WHERE association.project_id = v_scope.project_id
                          AND association.pool_id = v_scope.pool_id;
                        IF NOT FOUND OR v_association_enabled IS DISTINCT FROM TRUE THEN
                            RETURN aap_inference_routing_v1._routing_rejected('pool_not_enabled');
                        END IF;
                    END IF;
                END IF;
                IF aap_inference_routing_v1._routing_has_control(v_scope.registry_id) THEN
                    RETURN aap_inference_routing_v1._routing_rejected('management_busy');
                END IF;
            END IF;
            INSERT INTO aap_inference_routing_v1.attempts (
                id, scope_id, method, path, transport_identity_sha256,
                state, status_code, reason
            ) VALUES (
                v_attempt_id, v_scope_id, v_method, v_path, v_identity_hash,
                'active', NULL, NULL
            );
            IF v_scope.state = 'reserved' THEN
                UPDATE aap_inference_routing_v1.scopes AS scope
                SET state = 'active', updated_at = pg_catalog.now()
                WHERE scope.id = v_scope_id;
            END IF;
        END IF;
        SELECT state.* INTO v_state
        FROM aap_inference_routing_v1.registry_state AS state
        WHERE state.registry_id = v_scope.registry_id;
        v_header_token := NULL;
        IF FOUND AND v_state.continuity = 'ready' THEN
            v_header_token := aap_inference_routing_v1._routing_sign_admission(
                v_scope.registry_id, v_scope_id, v_attempt_id, v_scope.operation,
                v_method, v_path
            );
        END IF;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'attempt_id', v_attempt_id,
            'scope', aap_inference_routing_v1._routing_scope_view(v_scope_id, true),
            'headers', CASE WHEN v_header_token IS NULL THEN '{}'::jsonb
                            ELSE pg_catalog.jsonb_build_object('X-AAP-Route-Admission', v_header_token) END
        ));

    WHEN 'settle_attempt' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['scope_id', 'capability', 'attempt_id', 'kind',
                          'status_code', 'reason']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing attempt settlement';
        END IF;
        v_scope_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'scope_id');
        v_attempt_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'attempt_id');
        v_capability := v_args ->> 'capability';
        v_finish_kind := v_args ->> 'kind';
        IF pg_catalog.jsonb_typeof(v_args -> 'capability') IS DISTINCT FROM 'string'
           OR v_capability !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(v_args -> 'kind') IS DISTINCT FROM 'string'
           OR v_finish_kind NOT IN ('response', 'uncertain') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing attempt settlement';
        END IF;
        IF v_args -> 'status_code' <> 'null'::jsonb THEN
            v_status_code := aap_inference_routing_v1._routing_integer(
                v_args -> 'status_code', 100, 599
            )::integer;
        ELSE
            v_status_code := NULL;
        END IF;
        IF v_args -> 'reason' <> 'null'::jsonb THEN
            IF pg_catalog.jsonb_typeof(v_args -> 'reason') IS DISTINCT FROM 'string'
               OR (v_args ->> 'reason') !~ '^[a-z0-9_]{1,96}$' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing attempt settlement';
            END IF;
            v_reason := v_args ->> 'reason';
        ELSE
            v_reason := NULL;
        END IF;
        IF (v_finish_kind = 'response' AND (v_status_code IS NULL OR v_reason IS NOT NULL))
           OR (v_finish_kind = 'uncertain' AND v_status_code IS NOT NULL) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing attempt settlement';
        END IF;
        SELECT scope.* INTO v_scope
        FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.id = v_scope_id
        FOR UPDATE;
        IF NOT FOUND OR aap_inference_routing_v1._routing_scope_capability(v_scope_id)
            IS DISTINCT FROM v_capability THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
        END IF;
        SELECT attempt.* INTO v_attempt
        FROM aap_inference_routing_v1.attempts AS attempt
        WHERE attempt.id = v_attempt_id
          AND attempt.scope_id = v_scope_id
        FOR UPDATE;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
        END IF;
        IF v_attempt.state = 'reconciled' THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
        ELSIF v_attempt.state = 'response' THEN
            IF v_finish_kind <> 'response'
               OR v_attempt.status_code IS DISTINCT FROM v_status_code
               OR v_attempt.reason IS DISTINCT FROM v_reason THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing attempt result conflict';
            END IF;
        ELSIF v_attempt.state = 'uncertain' AND v_finish_kind = 'uncertain' THEN
            IF v_attempt.reason IS DISTINCT FROM v_reason THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing attempt result conflict';
            END IF;
        ELSE
            UPDATE aap_inference_routing_v1.attempts AS attempt
            SET state = v_finish_kind,
                status_code = CASE WHEN v_finish_kind = 'response' THEN v_status_code ELSE NULL END,
                reason = CASE WHEN v_finish_kind = 'uncertain' THEN v_reason ELSE NULL END,
                updated_at = pg_catalog.now()
            WHERE attempt.id = v_attempt_id;
        END IF;
        IF EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.attempts AS attempt
            WHERE attempt.scope_id = v_scope_id AND attempt.state IN ('active', 'uncertain')
        ) THEN
            UPDATE aap_inference_routing_v1.scopes AS scope
            SET state = CASE WHEN v_scope.closed THEN 'uncertain' ELSE 'uncertain' END,
                updated_at = pg_catalog.now()
            WHERE scope.id = v_scope_id
              AND scope.state NOT IN ('cancelled', 'expired', 'reconciled');
        ELSIF v_scope.closed THEN
            UPDATE aap_inference_routing_v1.scopes AS scope
            SET state = CASE WHEN scope.state = 'cancelled' THEN 'cancelled' ELSE 'finished' END,
                updated_at = pg_catalog.now()
            WHERE scope.id = v_scope_id
              AND scope.state NOT IN ('expired', 'reconciled');
        ELSIF v_scope.state = 'uncertain' THEN
            UPDATE aap_inference_routing_v1.scopes AS scope
            SET state = 'active', updated_at = pg_catalog.now()
            WHERE scope.id = v_scope_id AND NOT scope.closed;
        END IF;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'attempt', pg_catalog.jsonb_build_object(
                'id', v_attempt_id,
                'state', v_finish_kind,
                'status_code', v_status_code,
                'reason', v_reason
            )
        ));

    WHEN 'heartbeat_scope' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['scope_id', 'capability', 'ttl_ms']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing heartbeat';
        END IF;
        v_scope_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'scope_id');
        v_capability := v_args ->> 'capability';
        v_ttl := aap_inference_routing_v1._routing_integer(v_args -> 'ttl_ms', 1, 2147483647);
        IF pg_catalog.jsonb_typeof(v_args -> 'capability') IS DISTINCT FROM 'string'
           OR v_capability !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing heartbeat';
        END IF;
        SELECT scope.* INTO v_scope
        FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.id = v_scope_id
        FOR UPDATE;
        IF NOT FOUND OR aap_inference_routing_v1._routing_scope_capability(v_scope_id)
            IS DISTINCT FROM v_capability THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
        END IF;
        IF v_scope.closed OR v_scope.state IN ('finished', 'cancelled', 'expired', 'reconciled') THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_closed');
        END IF;
        IF v_scope.state = 'reserved' AND v_scope.heartbeat_deadline <= pg_catalog.clock_timestamp()
           AND NOT EXISTS (
               SELECT 1 FROM aap_inference_routing_v1.attempts AS attempt
               WHERE attempt.scope_id = v_scope_id
           ) THEN
            UPDATE aap_inference_routing_v1.scopes AS scope
            SET state = 'expired', closed = true, updated_at = pg_catalog.now()
            WHERE scope.id = v_scope_id;
            RETURN aap_inference_routing_v1._routing_rejected('scope_expired');
        END IF;
        UPDATE aap_inference_routing_v1.scopes AS scope
        SET heartbeat_deadline = pg_catalog.clock_timestamp() + v_ttl * interval '1 millisecond',
            updated_at = pg_catalog.now()
        WHERE scope.id = v_scope_id;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'scope', aap_inference_routing_v1._routing_scope_view(v_scope_id, true)
        ));

    WHEN 'close_scope' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['scope_id', 'capability', 'outcome']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing scope close';
        END IF;
        v_scope_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'scope_id');
        v_capability := v_args ->> 'capability';
        v_finish_kind := v_args ->> 'outcome';
        IF pg_catalog.jsonb_typeof(v_args -> 'capability') IS DISTINCT FROM 'string'
           OR v_capability !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(v_args -> 'outcome') IS DISTINCT FROM 'string'
           OR v_finish_kind NOT IN ('success', 'error', 'cancel') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing scope close';
        END IF;
        SELECT scope.* INTO v_scope
        FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.id = v_scope_id
        FOR UPDATE;
        IF NOT FOUND OR aap_inference_routing_v1._routing_scope_capability(v_scope_id)
            IS DISTINCT FROM v_capability THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
        END IF;
        IF v_scope.state IN ('finished', 'cancelled', 'expired', 'reconciled') THEN
            RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
                'scope', aap_inference_routing_v1._routing_scope_view(v_scope_id, true)
            ));
        END IF;
        UPDATE aap_inference_routing_v1.attempts AS attempt
        SET state = 'uncertain', status_code = NULL,
            reason = COALESCE(attempt.reason, 'scope_closed'),
            updated_at = pg_catalog.now()
        WHERE attempt.scope_id = v_scope_id
          AND attempt.state = 'active';
        IF EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.attempts AS attempt
            WHERE attempt.scope_id = v_scope_id AND attempt.state IN ('active', 'uncertain')
        ) THEN
            v_finish_kind := 'uncertain';
        ELSIF v_finish_kind = 'cancel' THEN
            v_finish_kind := 'cancelled';
        ELSE
            v_finish_kind := 'finished';
        END IF;
        UPDATE aap_inference_routing_v1.scopes AS scope
        SET state = v_finish_kind, closed = true, updated_at = pg_catalog.now()
        WHERE scope.id = v_scope_id;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'scope', aap_inference_routing_v1._routing_scope_view(v_scope_id, true)
        ));

    WHEN 'quiescence' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(v_args, ARRAY['registry_id']) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing quiescence request';
        END IF;
        v_registry_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'registry_id');
        BEGIN
            PERFORM registry.id
            FROM public.ml_backend_registry AS registry
            WHERE registry.id = v_registry_id
            FOR UPDATE NOWAIT;
            IF NOT FOUND THEN
                RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
            END IF;
            SELECT member.id, member.pool_id, member.traffic_state
            INTO v_member
            FROM public.ml_backend_pool_members AS member
            WHERE member.registry_id = v_registry_id;
            IF FOUND THEN
                v_member_id := v_member.id;
                v_pool_id := v_member.pool_id;
                PERFORM pool.id
                FROM public.ml_backend_service_pools AS pool
                WHERE pool.id = v_pool_id
                FOR UPDATE NOWAIT;
                IF NOT FOUND THEN
                    RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
                END IF;
                SELECT member.id, member.pool_id, member.registry_id,
                       member.traffic_state, pool.routing_generation
                INTO v_member
                FROM public.ml_backend_pool_members AS member
                JOIN public.ml_backend_service_pools AS pool ON pool.id = member.pool_id
                WHERE member.registry_id = v_registry_id
                FOR SHARE OF member;
                IF NOT FOUND OR v_member.pool_id IS DISTINCT FROM v_pool_id THEN
                    RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
                END IF;
                v_member_id := v_member.id;
                v_generation := v_member.routing_generation;
                v_reason := v_member.traffic_state;
            ELSE
                v_pool_id := NULL;
                v_member_id := NULL;
                v_generation := NULL;
                v_reason := NULL;
            END IF;
        EXCEPTION WHEN lock_not_available THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END;
        SELECT state.continuity, state.realm
        INTO v_state
        FROM aap_inference_routing_v1.registry_state AS state
        WHERE state.registry_id = v_registry_id;
        IF NOT FOUND OR v_state.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm() THEN
            RETURN aap_inference_routing_v1._routing_rejected('backend_not_ready');
        END IF;
        v_now := pg_catalog.clock_timestamp();
        PERFORM aap_inference_routing_v1._routing_has_unresolved(v_registry_id);
        SELECT pg_catalog.count(*) FILTER (
                   WHERE scope.state = 'active'
                      OR (scope.state = 'reserved' AND scope.heartbeat_deadline > v_now)
               ),
               pg_catalog.count(*) FILTER (WHERE scope.state = 'uncertain')
        INTO v_active_count, v_uncertain_count
        FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.registry_id = v_registry_id;
        SELECT control.id INTO v_control_id
        FROM aap_inference_routing_v1.controls AS control
        WHERE control.registry_id = v_registry_id
          AND control.phase IN ('prepared', 'draining', 'uncertain');
        v_topology := pg_catalog.jsonb_build_object(
            'registry_id', v_registry_id,
            'member_id', v_member_id,
            'pool_id', v_pool_id,
            'routing_generation', v_generation,
            'traffic_state', v_reason
        );
        IF v_control_id IS NOT NULL THEN
            RETURN aap_inference_routing_v1._routing_reply(
                'rejected', pg_catalog.jsonb_build_object(
                    'reason', 'management_busy', 'control_id', v_control_id
                )
            );
        END IF;
        IF v_pool_id IS NOT NULL AND v_reason IS DISTINCT FROM 'draining' THEN
            RETURN aap_inference_routing_v1._routing_rejected('member_not_draining');
        END IF;
        IF v_active_count > 0 OR v_uncertain_count > 0 THEN
            RETURN aap_inference_routing_v1._routing_rejected('member_not_quiescent');
        END IF;
        IF v_state.continuity IS DISTINCT FROM 'ready' THEN
            RETURN aap_inference_routing_v1._routing_rejected('continuity_unverified');
        END IF;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'topology', v_topology,
            'active_scopes', v_active_count,
            'uncertain_scopes', v_uncertain_count,
            'continuity', v_state.continuity,
            'control_id', NULL
        ));

    WHEN 'prepare_control' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['control_id', 'registry_id', 'kind', 'expected_topology']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing control request';
        END IF;
        v_control_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'control_id');
        v_registry_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'registry_id');
        v_operation := v_args ->> 'kind';
        IF pg_catalog.jsonb_typeof(v_args -> 'kind') IS DISTINCT FROM 'string'
           OR v_operation NOT IN ('unload', 'reload') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing control request';
        END IF;
        v_expected_topology := v_args -> 'expected_topology';
        IF v_expected_topology <> 'null'::jsonb AND NOT aap_inference_routing_v1._routing_exact_keys(
            v_expected_topology, ARRAY['registry_id', 'member_id', 'pool_id',
                                       'routing_generation', 'traffic_state']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing control topology';
        END IF;
        SELECT control.* INTO v_control
        FROM aap_inference_routing_v1.controls AS control
        WHERE control.id = v_control_id
        FOR UPDATE;
        IF FOUND THEN
            IF v_control.registry_id IS DISTINCT FROM v_registry_id
               OR v_control.kind IS DISTINCT FROM v_operation
               OR v_control.expected_topology IS DISTINCT FROM NULLIF(v_expected_topology, 'null'::jsonb) THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing control identity conflict';
            END IF;
            v_control_view := aap_inference_routing_v1._routing_control_view(v_control_id, true);
            RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
                'control_id', v_control_view -> 'id',
                'registry_id', v_control_view -> 'registry_id',
                'kind', v_control_view -> 'kind',
                'phase', v_control_view -> 'phase',
                'capability', v_control_view -> 'capability'
            ));
        END IF;
        BEGIN
            PERFORM registry.id
            FROM public.ml_backend_registry AS registry
            WHERE registry.id = v_registry_id
            FOR UPDATE NOWAIT;
            IF NOT FOUND THEN
                RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
            END IF;
            SELECT member.id, member.pool_id, member.traffic_state
            INTO v_member
            FROM public.ml_backend_pool_members AS member
            WHERE member.registry_id = v_registry_id;
            IF FOUND THEN
                v_member_id := v_member.id;
                v_pool_id := v_member.pool_id;
                PERFORM pool.id
                FROM public.ml_backend_service_pools AS pool
                WHERE pool.id = v_pool_id
                FOR UPDATE NOWAIT;
                IF NOT FOUND THEN
                    RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
                END IF;
                SELECT member.id, member.pool_id, member.registry_id,
                       member.traffic_state, pool.routing_generation
                INTO v_member
                FROM public.ml_backend_pool_members AS member
                JOIN public.ml_backend_service_pools AS pool ON pool.id = member.pool_id
                WHERE member.registry_id = v_registry_id
                FOR SHARE OF member;
                IF NOT FOUND OR v_member.pool_id IS DISTINCT FROM v_pool_id THEN
                    RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
                END IF;
                v_member_id := v_member.id;
                v_generation := v_member.routing_generation;
                v_reason := v_member.traffic_state;
            ELSE
                v_pool_id := NULL;
                v_member_id := NULL;
                v_generation := NULL;
                v_reason := NULL;
            END IF;
        EXCEPTION WHEN lock_not_available THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END;
        v_topology := pg_catalog.jsonb_build_object(
            'registry_id', v_registry_id,
            'member_id', v_member_id,
            'pool_id', v_pool_id,
            'routing_generation', v_generation,
            'traffic_state', v_reason
        );
        IF v_expected_topology <> 'null'::jsonb
           AND v_expected_topology IS DISTINCT FROM v_topology THEN
            RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
        END IF;
        IF v_pool_id IS NOT NULL AND (
            (v_operation = 'unload' AND v_reason IS DISTINCT FROM 'draining')
            OR (v_operation = 'reload' AND v_reason NOT IN ('disabled', 'draining'))
        ) THEN
            RETURN aap_inference_routing_v1._routing_rejected('member_not_draining');
        END IF;
        PERFORM aap_inference_routing_v1._routing_has_unresolved(v_registry_id);
        IF EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.scopes AS scope
            WHERE scope.registry_id = v_registry_id
              AND (scope.state IN ('active', 'uncertain')
                   OR (scope.state = 'reserved' AND scope.heartbeat_deadline > pg_catalog.clock_timestamp()))
        ) THEN
            RETURN aap_inference_routing_v1._routing_rejected('member_not_quiescent');
        END IF;
        SELECT state.* INTO v_state
        FROM aap_inference_routing_v1.registry_state AS state
        WHERE state.registry_id = v_registry_id;
        IF NOT FOUND OR v_state.continuity IS DISTINCT FROM 'ready'
           OR v_state.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm() THEN
            RETURN aap_inference_routing_v1._routing_rejected('continuity_unverified');
        END IF;
        IF aap_inference_routing_v1._routing_has_control(v_registry_id) THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END IF;
        INSERT INTO aap_inference_routing_v1.controls (
            id, registry_id, kind, phase, new_epoch, challenge,
            retired_epoch, backend_incarnation, config_revision, realm, expected_topology
        ) VALUES (
            v_control_id, v_registry_id, v_operation, 'prepared', NULL,
            pg_catalog.encode(public.gen_random_bytes(32), 'hex'), NULL,
            v_state.backend_incarnation, v_state.config_revision, v_state.realm,
            NULLIF(v_expected_topology, 'null'::jsonb)
        );
        v_control_view := aap_inference_routing_v1._routing_control_view(v_control_id, true);
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'control_id', v_control_view -> 'id',
            'registry_id', v_control_view -> 'registry_id',
            'kind', v_control_view -> 'kind',
            'phase', v_control_view -> 'phase',
            'capability', v_control_view -> 'capability'
        ));

    WHEN 'finish_control' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['control_id', 'capability']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing control finish';
        END IF;
        v_control_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'control_id');
        v_capability := v_args ->> 'capability';
        IF pg_catalog.jsonb_typeof(v_args -> 'capability') IS DISTINCT FROM 'string'
           OR v_capability !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing control finish';
        END IF;
        SELECT control.* INTO v_control
        FROM aap_inference_routing_v1.controls AS control
        WHERE control.id = v_control_id
        FOR UPDATE;
        IF NOT FOUND OR aap_inference_routing_v1._routing_control_capability(v_control_id)
            IS DISTINCT FROM v_capability THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END IF;
        IF v_control.kind = 'epoch' THEN
            RETURN aap_inference_routing_v1._routing_rejected('invalid_command');
        END IF;
        IF v_control.phase = 'completed' THEN
            v_control_view := aap_inference_routing_v1._routing_control_view(v_control_id, false);
            RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
                'control_id', v_control_view -> 'id',
                'registry_id', v_control_view -> 'registry_id',
                'kind', v_control_view -> 'kind',
                'phase', v_control_view -> 'phase'
            ));
        END IF;
        SELECT pg_catalog.count(*) FILTER (WHERE attempt.state = 'active'),
               pg_catalog.count(*) FILTER (WHERE attempt.state = 'uncertain'),
               pg_catalog.count(*) FILTER (WHERE attempt.state = 'response'),
               COALESCE(pg_catalog.bool_or(
                   attempt.state = 'response'
                   AND (attempt.status_code < 200 OR attempt.status_code >= 300)
               ), false)
        INTO v_active_count, v_uncertain_count, v_response_count, v_failed_response
        FROM aap_inference_routing_v1.control_attempts AS attempt
        WHERE attempt.control_id = v_control_id;
        IF v_active_count > 0 THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END IF;
        -- With no attempts, no admission JWT was issued and the hold can cancel.
        UPDATE aap_inference_routing_v1.controls AS control
        SET phase = CASE
                WHEN v_uncertain_count > 0 OR v_failed_response THEN 'uncertain'
                ELSE 'completed'
            END,
            updated_at = pg_catalog.now()
        WHERE control.id = v_control_id;
        v_control_view := aap_inference_routing_v1._routing_control_view(v_control_id, false);
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'control_id', v_control_view -> 'id',
            'registry_id', v_control_view -> 'registry_id',
            'kind', v_control_view -> 'kind',
            'phase', v_control_view -> 'phase'
        ));

    WHEN 'prepare_epoch' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['control_id', 'registry_id']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing epoch request';
        END IF;
        v_control_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'control_id');
        v_registry_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'registry_id');
        BEGIN
            PERFORM registry.id
            FROM public.ml_backend_registry AS registry
            WHERE registry.id = v_registry_id
            FOR UPDATE NOWAIT;
            IF NOT FOUND THEN
                RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
            END IF;
            SELECT member.id, member.pool_id
            INTO v_member
            FROM public.ml_backend_pool_members AS member
            WHERE member.registry_id = v_registry_id;
            IF FOUND THEN
                v_pool_id := v_member.pool_id;
                PERFORM pool.id
                FROM public.ml_backend_service_pools AS pool
                WHERE pool.id = v_pool_id
                FOR UPDATE NOWAIT;
                IF NOT FOUND THEN
                    RETURN aap_inference_routing_v1._routing_rejected('pool_unavailable');
                END IF;
                SELECT member.id, member.pool_id
                INTO v_member
                FROM public.ml_backend_pool_members AS member
                WHERE member.registry_id = v_registry_id
                  AND member.pool_id = v_pool_id
                FOR SHARE NOWAIT;
                IF NOT FOUND THEN
                    RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
                END IF;
            ELSE
                v_pool_id := NULL;
            END IF;
        EXCEPTION WHEN lock_not_available THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END;
        SELECT registry.* INTO v_registry
        FROM public.ml_backend_registry AS registry
        WHERE registry.id = v_registry_id;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
        END IF;
        SELECT state.* INTO v_state
        FROM aap_inference_routing_v1.registry_state AS state
        WHERE state.registry_id = v_registry_id;
        IF NOT FOUND
           OR v_state.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
           OR aap_inference_routing_v1._routing_secret() IS NULL THEN
            RETURN aap_inference_routing_v1._routing_rejected('backend_not_ready');
        END IF;
        IF v_registry.auth_method IS DISTINCT FROM 'token'
           OR v_registry.auth_token IS NULL
           OR pg_catalog.char_length(v_registry.auth_token) NOT BETWEEN 1 AND 8192
           OR v_registry.auth_token !~ '^[!-~]+$' THEN
            RETURN aap_inference_routing_v1._routing_rejected('backend_not_ready');
        END IF;

        SELECT control.* INTO v_control
        FROM aap_inference_routing_v1.controls AS control
        WHERE control.id = v_control_id
        FOR UPDATE;
        v_found_candidate := FOUND;
        IF v_found_candidate THEN
            IF v_control.registry_id IS DISTINCT FROM v_registry_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing control identity conflict';
            END IF;
            IF v_control.phase = 'completed' THEN
                RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
            END IF;
            IF v_control.phase NOT IN ('prepared', 'draining', 'uncertain') THEN
                RETURN aap_inference_routing_v1._routing_rejected('invalid_command');
            END IF;
            IF v_control.kind NOT IN ('epoch', 'unload', 'reload') THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing control identity conflict';
            END IF;
            IF v_control.realm IS DISTINCT FROM v_state.realm
               OR v_control.config_revision IS DISTINCT FROM v_state.config_revision THEN
                RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
            END IF;
        ELSIF aap_inference_routing_v1._routing_has_control(v_registry_id) THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END IF;

        IF EXISTS (
            SELECT 1
            FROM aap_inference_routing_v1.scopes AS scope
            WHERE scope.registry_id = v_registry_id
              AND (scope.state = 'active'
                   OR (scope.state = 'reserved'
                       AND scope.heartbeat_deadline > pg_catalog.clock_timestamp())
                   OR (scope.state = 'uncertain' AND NOT scope.closed))
        ) THEN
            RETURN aap_inference_routing_v1._routing_rejected('member_not_quiescent');
        END IF;

        IF v_found_candidate THEN
            IF v_control.kind IN ('unload', 'reload') THEN
                v_new_epoch := public.gen_random_uuid();
                v_retired_epoch := CASE WHEN v_state.continuity = 'ready'
                                        THEN v_state.authority_epoch ELSE NULL END;
                v_challenge := pg_catalog.encode(public.gen_random_bytes(32), 'hex');
                UPDATE aap_inference_routing_v1.controls AS control
                SET kind = 'epoch',
                    recovered_kind = v_control.kind,
                    phase = 'prepared',
                    new_epoch = v_new_epoch,
                    retired_epoch = v_retired_epoch,
                    challenge = v_challenge,
                    backend_incarnation = v_state.backend_incarnation,
                    config_revision = v_state.config_revision,
                    realm = v_state.realm,
                    updated_at = pg_catalog.now()
                WHERE control.id = v_control_id;
                SELECT control.* INTO v_control
                FROM aap_inference_routing_v1.controls AS control
                WHERE control.id = v_control_id;
            END IF;
        ELSE
            v_new_epoch := public.gen_random_uuid();
            v_retired_epoch := CASE WHEN v_state.continuity = 'ready'
                                    THEN v_state.authority_epoch ELSE NULL END;
            v_challenge := pg_catalog.encode(public.gen_random_bytes(32), 'hex');
            INSERT INTO aap_inference_routing_v1.controls (
                id, registry_id, kind, phase, new_epoch, retired_epoch, challenge,
                backend_incarnation, config_revision, realm, expected_topology
            ) VALUES (
                v_control_id, v_registry_id, 'epoch', 'prepared', v_new_epoch,
                v_retired_epoch, v_challenge, v_state.backend_incarnation,
                v_state.config_revision, v_state.realm, NULL
            );
            SELECT * INTO v_control
            FROM aap_inference_routing_v1.controls AS control
            WHERE control.id = v_control_id;
        END IF;
        SELECT registry.* INTO v_registry
        FROM public.ml_backend_registry AS registry
        WHERE registry.id = v_registry_id;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
        END IF;
        SELECT state.* INTO v_state
        FROM aap_inference_routing_v1.registry_state AS state
        WHERE state.registry_id = v_registry_id;
        IF NOT FOUND OR v_state.realm IS DISTINCT FROM v_control.realm
           OR v_state.config_revision IS DISTINCT FROM v_control.config_revision
           OR v_control.phase = 'completed' THEN
            RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
        END IF;
        v_secret := aap_inference_routing_v1._routing_secret();
        v_epoch_key := public.hmac(
            pg_catalog.convert_to(
                'aap-inference-routing-epoch-key/v1:' || v_control.realm || ':' ||
                v_control.id::text || ':' || v_registry_id::text || ':' ||
                v_control.new_epoch::text || ':' || v_control.challenge,
                'UTF8'
            ),
            v_secret,
            'sha256'
        );
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'control_id', v_control.id,
            'registry_id', v_registry_id,
            'phase', v_control.phase,
            'new_epoch', v_control.new_epoch,
            'challenge', v_control.challenge,
            'retired_epoch', v_control.retired_epoch,
            'backend_incarnation', v_control.backend_incarnation,
            'endpoint', pg_catalog.jsonb_build_object(
                'url', v_registry.url,
                'auth_method', v_registry.auth_method,
                'auth_token', v_registry.auth_token
            ),
            'signing_key', aap_inference_routing_v1._routing_base64url(v_epoch_key)
        ));

    WHEN 'finish_epoch' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(v_args, ARRAY['control_id', 'reply'])
           OR NOT aap_inference_routing_v1._routing_exact_keys(
               v_args -> 'reply', ARRAY['schema', 'command_id', 'registry_id',
                                        'authority_epoch',
                                        'backend_incarnation', 'challenge',
                                        'phase', 'active_requests']
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend epoch reply';
        END IF;
        v_control_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'control_id');
        v_reply := v_args -> 'reply';
        IF pg_catalog.jsonb_typeof(v_reply -> 'schema') IS DISTINCT FROM 'string'
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-backend-route-control-reply/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'command_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'registry_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'authority_epoch') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'backend_incarnation') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'challenge') IS DISTINCT FROM 'string'
           OR v_reply ->> 'phase' IS DISTINCT FROM 'bound' THEN
            RETURN aap_inference_routing_v1._routing_rejected('invalid_command');
        END IF;
        v_reply_command := aap_inference_routing_v1._routing_uuid(v_reply ->> 'command_id');
        v_reply_registry := aap_inference_routing_v1._routing_uuid(v_reply ->> 'registry_id');
        v_reply_epoch := aap_inference_routing_v1._routing_uuid(v_reply ->> 'authority_epoch');
        v_reply_incarnation := aap_inference_routing_v1._routing_uuid(v_reply ->> 'backend_incarnation');
        v_status_code := aap_inference_routing_v1._routing_integer(
            v_reply -> 'active_requests', 0, 2147483647
        )::integer;
        SELECT control.* INTO v_control
        FROM aap_inference_routing_v1.controls AS control
        WHERE control.id = v_control_id
        FOR UPDATE;
        IF NOT FOUND OR v_control.kind IS DISTINCT FROM 'epoch'
           OR v_control.phase NOT IN ('prepared', 'draining', 'uncertain')
           OR v_control.id IS DISTINCT FROM v_reply_command
           OR v_control.registry_id IS DISTINCT FROM v_reply_registry
           OR v_control.new_epoch IS DISTINCT FROM v_reply_epoch
           OR v_control.challenge IS DISTINCT FROM (v_reply ->> 'challenge') THEN
            RETURN aap_inference_routing_v1._routing_rejected('invalid_command');
        END IF;
        IF v_status_code <> 0 THEN
            RETURN aap_inference_routing_v1._routing_rejected('member_not_quiescent');
        END IF;
        BEGIN
            PERFORM registry.id
            FROM public.ml_backend_registry AS registry
            WHERE registry.id = v_control.registry_id
            FOR SHARE NOWAIT;
            IF NOT FOUND THEN
                RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
            END IF;
            SELECT member.pool_id INTO v_pool_id
            FROM public.ml_backend_pool_members AS member
            WHERE member.registry_id = v_control.registry_id;
            IF FOUND THEN
                PERFORM pool.id
                FROM public.ml_backend_service_pools AS pool
                WHERE pool.id = v_pool_id
                FOR UPDATE NOWAIT;
                IF NOT FOUND THEN
                    RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
                END IF;
                PERFORM member.id
                FROM public.ml_backend_pool_members AS member
                WHERE member.pool_id = v_pool_id
                  AND member.registry_id = v_control.registry_id
                FOR SHARE NOWAIT;
            END IF;
        EXCEPTION WHEN lock_not_available THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END;
        IF EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.scopes AS scope
            WHERE scope.registry_id = v_control.registry_id
              AND (scope.state = 'active'
                   OR (scope.state = 'reserved'
                       AND scope.heartbeat_deadline > pg_catalog.clock_timestamp())
                   OR (scope.state = 'uncertain' AND NOT scope.closed))
        ) THEN
            RETURN aap_inference_routing_v1._routing_rejected('member_not_quiescent');
        END IF;
        SELECT state.* INTO v_state
        FROM aap_inference_routing_v1.registry_state AS state
        WHERE state.registry_id = v_control.registry_id;
        IF NOT FOUND OR v_state.config_revision IS DISTINCT FROM v_control.config_revision
           OR v_state.realm IS DISTINCT FROM v_control.realm
           OR v_state.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm() THEN
            RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
        END IF;
        v_secret := aap_inference_routing_v1._routing_secret();
        v_epoch_key := public.hmac(
            pg_catalog.convert_to(
                'aap-inference-routing-epoch-key/v1:' || v_control.realm || ':' ||
                v_control.id::text || ':' || v_control.registry_id::text || ':' ||
                v_control.new_epoch::text || ':' || v_control.challenge,
                'UTF8'
            ),
            v_secret,
            'sha256'
        );
        UPDATE aap_inference_routing_v1.registry_state AS state
        SET authority_epoch = v_control.new_epoch,
            backend_incarnation = v_reply_incarnation,
            continuity = 'ready',
            epoch_key = v_epoch_key,
            updated_at = pg_catalog.now()
        WHERE state.registry_id = v_control.registry_id;
        UPDATE aap_inference_routing_v1.attempts AS attempt
        SET state = 'reconciled', updated_at = pg_catalog.now()
        FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.id = attempt.scope_id
          AND scope.registry_id = v_control.registry_id
          AND scope.state = 'uncertain'
          AND scope.closed IS TRUE
          AND attempt.state = 'uncertain';
        UPDATE aap_inference_routing_v1.scopes AS scope
        SET state = 'reconciled', reconciled_by = v_control_id,
            updated_at = pg_catalog.now()
        WHERE scope.registry_id = v_control.registry_id
          AND scope.state = 'uncertain'
          AND scope.closed IS TRUE;
        IF v_control.recovered_kind IS NOT NULL THEN
            UPDATE aap_inference_routing_v1.control_attempts AS attempt
            SET state = 'reconciled', updated_at = pg_catalog.now()
            WHERE attempt.control_id = v_control_id
              AND attempt.state IN ('active', 'uncertain');
        END IF;
        UPDATE aap_inference_routing_v1.controls AS control
        SET phase = 'completed', backend_incarnation = v_reply_incarnation,
            updated_at = pg_catalog.now()
        WHERE control.id = v_control_id;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'control_id', v_control_id,
            'registry_id', v_control.registry_id,
            'phase', 'completed',
            'authority_epoch', v_control.new_epoch,
            'backend_incarnation', v_reply_incarnation
        ));

    WHEN 'capture_transport_subject' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['parent_kind', 'parent_id', 'capability', 'attempt_id',
                          'transport_identity']
        ) OR NOT aap_inference_routing_v1._routing_exact_keys(
            v_args -> 'transport_identity',
            ARRAY['url', 'auth_method', 'auth_token', 'gpu_resource_id', 'extra_params']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid transport subject';
        END IF;
        v_operation := v_args ->> 'parent_kind';
        IF pg_catalog.jsonb_typeof(v_args -> 'parent_kind') IS DISTINCT FROM 'string'
           OR v_operation NOT IN ('scope', 'control') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid transport subject';
        END IF;
        v_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'parent_id');
        v_attempt_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'attempt_id');
        v_capability := v_args ->> 'capability';
        v_transport := v_args -> 'transport_identity';
        IF pg_catalog.jsonb_typeof(v_args -> 'capability') IS DISTINCT FROM 'string'
           OR v_capability !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(v_transport -> 'url') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_transport -> 'auth_method') IS DISTINCT FROM 'string'
           OR (pg_catalog.jsonb_typeof(v_transport -> 'auth_token') NOT IN ('string', 'null'))
           OR (pg_catalog.jsonb_typeof(v_transport -> 'gpu_resource_id') NOT IN ('string', 'null')) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid transport subject';
        END IF;
        IF v_operation = 'scope' THEN
            SELECT scope.registry_id, scope.realm, scope.state, scope.closed
            INTO v_scope
            FROM aap_inference_routing_v1.scopes AS scope
            WHERE scope.id = v_id;
            IF NOT FOUND OR aap_inference_routing_v1._routing_scope_capability(v_id)
                IS DISTINCT FROM v_capability
               OR v_scope.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
               OR v_scope.closed OR v_scope.state NOT IN ('reserved', 'active') THEN
                RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
            END IF;
            v_registry_id := v_scope.registry_id;
            IF EXISTS (SELECT 1 FROM aap_inference_routing_v1.attempts AS attempt
                       WHERE attempt.id = v_attempt_id) THEN
                RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
            END IF;
        ELSE
            SELECT control.registry_id, control.realm, control.kind, control.phase
            INTO v_control
            FROM aap_inference_routing_v1.controls AS control
            WHERE control.id = v_id;
            IF NOT FOUND OR aap_inference_routing_v1._routing_control_capability(v_id)
                IS DISTINCT FROM v_capability
               OR v_control.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
               OR v_control.kind NOT IN ('unload', 'reload')
               OR v_control.phase <> 'prepared' THEN
                RETURN aap_inference_routing_v1._routing_rejected('management_busy');
            END IF;
            v_registry_id := v_control.registry_id;
            IF EXISTS (SELECT 1 FROM aap_inference_routing_v1.control_attempts AS attempt
                       WHERE attempt.id = v_attempt_id) THEN
                RETURN aap_inference_routing_v1._routing_rejected('management_busy');
            END IF;
        END IF;
        v_secret := aap_inference_routing_v1._routing_secret();
        IF v_secret IS NULL THEN
            RETURN aap_inference_routing_v1._routing_rejected('backend_not_ready');
        END IF;
        v_identity_hash := pg_catalog.encode(
            public.digest(pg_catalog.convert_to(v_transport::text, 'UTF8'), 'sha256'),
            'hex'
        );
        v_origin_xid := pg_catalog.pg_current_xact_id();
        v_plaintext := pg_catalog.jsonb_build_object(
            'parent_kind', v_operation,
            'parent_id', v_id,
            'attempt_id', v_attempt_id,
            'identity_sha256', v_identity_hash,
            'origin_xid', v_origin_xid::text,
            'realm', aap_inference_routing_v1._routing_realm()
        )::text;
        v_ciphertext := public.pgp_sym_encrypt(
            v_plaintext,
            pg_catalog.encode(v_secret, 'hex'),
            'cipher-algo=aes256, compress-algo=0'
        );
        v_subject := 'v1.' || aap_inference_routing_v1._routing_base64url(v_ciphertext);
        RETURN aap_inference_routing_v1._routing_reply('ok',
            pg_catalog.jsonb_build_object('subject', v_subject));

    WHEN 'open_transport_subject' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['parent_kind', 'parent_id', 'attempt_id', 'subject']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid transport subject';
        END IF;
        v_operation := v_args ->> 'parent_kind';
        IF pg_catalog.jsonb_typeof(v_args -> 'parent_kind') IS DISTINCT FROM 'string'
           OR v_operation NOT IN ('scope', 'control')
           OR pg_catalog.jsonb_typeof(v_args -> 'subject') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid transport subject';
        END IF;
        v_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'parent_id');
        v_attempt_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'attempt_id');
        v_subject := v_args ->> 'subject';
        IF v_subject !~ '^v1\.[A-Za-z0-9_-]+$'
           OR pg_catalog.octet_length(v_subject) > 1048576 THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid transport subject';
        END IF;
        v_secret := aap_inference_routing_v1._routing_secret();
        IF v_secret IS NULL THEN
            RETURN aap_inference_routing_v1._routing_rejected('backend_not_ready');
        END IF;
        BEGIN
            v_ciphertext := aap_inference_routing_v1._routing_unbase64url(
                pg_catalog.substr(v_subject, 4)
            );
            v_plaintext := public.pgp_sym_decrypt(
                v_ciphertext, pg_catalog.encode(v_secret, 'hex')
            );
            v_subject_json := v_plaintext::jsonb;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid transport subject';
        END;
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_subject_json, ARRAY['parent_kind', 'parent_id', 'attempt_id',
                                  'identity_sha256', 'origin_xid', 'realm']
        ) OR v_subject_json ->> 'parent_kind' IS DISTINCT FROM v_operation
          OR v_subject_json ->> 'parent_id' IS DISTINCT FROM v_id::text
          OR v_subject_json ->> 'attempt_id' IS DISTINCT FROM v_attempt_id::text
          OR v_subject_json ->> 'realm' IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
          OR (v_subject_json ->> 'identity_sha256') !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid transport subject';
        END IF;
        BEGIN
            v_origin_xid := (v_subject_json ->> 'origin_xid')::xid8;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid transport subject';
        END;
        IF pg_catalog.pg_xact_status(v_origin_xid) IS DISTINCT FROM 'in progress' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid transport subject';
        END IF;
        IF v_operation = 'scope' THEN
            SELECT scope.registry_id, scope.realm, scope.state, scope.closed
            INTO v_scope
            FROM aap_inference_routing_v1.scopes AS scope
            WHERE scope.id = v_id;
            IF NOT FOUND OR v_scope.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
               OR v_scope.closed OR v_scope.state NOT IN ('reserved', 'active')
               OR EXISTS (SELECT 1 FROM aap_inference_routing_v1.attempts AS attempt
                          WHERE attempt.id = v_attempt_id) THEN
                RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
            END IF;
        ELSE
            SELECT control.registry_id, control.realm, control.kind, control.phase
            INTO v_control
            FROM aap_inference_routing_v1.controls AS control
            WHERE control.id = v_id;
            IF NOT FOUND OR v_control.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
               OR v_control.kind NOT IN ('unload', 'reload') OR v_control.phase <> 'prepared'
               OR EXISTS (SELECT 1 FROM aap_inference_routing_v1.control_attempts AS attempt
                          WHERE attempt.id = v_attempt_id) THEN
                RETURN aap_inference_routing_v1._routing_rejected('management_busy');
            END IF;
        END IF;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'transport_identity', pg_catalog.jsonb_build_object(
                'identity_sha256', v_subject_json ->> 'identity_sha256'
            )
        ));

    WHEN 'begin_control_attempt' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['control_id', 'capability', 'attempt_id', 'method', 'path',
                          'transport_identity']
        ) OR NOT aap_inference_routing_v1._routing_exact_keys(
            v_args -> 'transport_identity', ARRAY['identity_sha256']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing control attempt';
        END IF;
        v_control_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'control_id');
        v_attempt_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'attempt_id');
        v_capability := v_args ->> 'capability';
        v_method := v_args ->> 'method';
        v_path := v_args ->> 'path';
        v_identity_hash := v_args #>> '{transport_identity,identity_sha256}';
        IF pg_catalog.jsonb_typeof(v_args -> 'capability') IS DISTINCT FROM 'string'
           OR v_capability !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(v_args -> 'method') IS DISTINCT FROM 'string'
           OR v_method IS DISTINCT FROM 'POST'
           OR pg_catalog.jsonb_typeof(v_args -> 'path') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_path) NOT BETWEEN 1 AND 4096
           OR v_path !~ '^/[^?#[:space:]]*$'
           OR pg_catalog.jsonb_typeof(v_args #> '{transport_identity,identity_sha256}')
                IS DISTINCT FROM 'string'
           OR v_identity_hash !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing control attempt';
        END IF;
        SELECT control.* INTO v_control
        FROM aap_inference_routing_v1.controls AS control
        WHERE control.id = v_control_id
        FOR UPDATE;
        IF NOT FOUND OR aap_inference_routing_v1._routing_control_capability(v_control_id)
            IS DISTINCT FROM v_capability THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END IF;
        IF v_control.kind NOT IN ('unload', 'reload') OR v_control.phase <> 'prepared' THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END IF;
        IF (v_control.kind = 'unload' AND v_path NOT IN ('/unload', '/lifecycle/unload'))
           OR (v_control.kind = 'reload' AND v_path <> '/reload') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing control path';
        END IF;
        SELECT attempt.* INTO v_attempt
        FROM aap_inference_routing_v1.control_attempts AS attempt
        WHERE attempt.id = v_attempt_id;
        IF FOUND THEN
            IF v_attempt.control_id IS DISTINCT FROM v_control_id
               OR v_attempt.method IS DISTINCT FROM v_method
               OR v_attempt.path IS DISTINCT FROM v_path
               OR v_attempt.transport_identity_sha256 IS DISTINCT FROM v_identity_hash THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing control attempt conflict';
            END IF;
            IF v_attempt.state <> 'active' THEN
                RETURN aap_inference_routing_v1._routing_rejected('management_busy');
            END IF;
        ELSE
            BEGIN
                PERFORM registry.id
                FROM public.ml_backend_registry AS registry
                WHERE registry.id = v_control.registry_id
                FOR SHARE NOWAIT;
                IF NOT FOUND THEN
                    RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
                END IF;
                SELECT state.* INTO v_state
                FROM aap_inference_routing_v1.registry_state AS state
                WHERE state.registry_id = v_control.registry_id;
                IF NOT FOUND OR v_state.realm IS DISTINCT FROM v_control.realm
                   OR v_state.config_revision IS DISTINCT FROM v_control.config_revision
                   OR v_state.continuity IS DISTINCT FROM 'ready' THEN
                    RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
                END IF;
                IF EXISTS (
                    SELECT 1 FROM aap_inference_routing_v1.control_attempts AS attempt
                    WHERE attempt.control_id = v_control_id
                      AND attempt.state IN ('active', 'uncertain')
                ) THEN
                    RETURN aap_inference_routing_v1._routing_rejected('management_busy');
                END IF;
                v_transport := aap_inference_routing_v1._routing_transport_identity(
                    v_control.registry_id
                );
                IF v_transport IS NULL
                   OR pg_catalog.encode(public.digest(
                       pg_catalog.convert_to(v_transport::text, 'UTF8'), 'sha256'
                   ), 'hex') IS DISTINCT FROM v_identity_hash THEN
                    RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
                END IF;
                IF EXISTS (
                    SELECT 1 FROM aap_inference_routing_v1.scopes AS scope
                    WHERE scope.registry_id = v_control.registry_id
                      AND (scope.state IN ('active', 'uncertain')
                           OR (scope.state = 'reserved'
                               AND scope.heartbeat_deadline > pg_catalog.clock_timestamp()))
                ) THEN
                    RETURN aap_inference_routing_v1._routing_rejected('management_busy');
                END IF;
            EXCEPTION WHEN lock_not_available THEN
                RETURN aap_inference_routing_v1._routing_rejected('management_busy');
            END;
            INSERT INTO aap_inference_routing_v1.control_attempts (
                id, control_id, method, path, transport_identity_sha256,
                state, status_code, reason
            ) VALUES (
                v_attempt_id, v_control_id, v_method, v_path, v_identity_hash,
                'active', NULL, NULL
            );
        END IF;
        v_header_token := aap_inference_routing_v1._routing_sign_admission(
            v_control.registry_id, v_control_id, v_attempt_id,
            'control:' || v_control.kind, v_method, v_path
        );
        IF v_header_token IS NULL THEN
            RETURN aap_inference_routing_v1._routing_rejected('continuity_unverified');
        END IF;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'attempt_id', v_attempt_id,
            'control', pg_catalog.jsonb_build_object(
                'control_id', v_control_id,
                'registry_id', v_control.registry_id,
                'kind', v_control.kind,
                'phase', v_control.phase,
                'capability', aap_inference_routing_v1._routing_control_capability(v_control_id)
            ),
            'headers', pg_catalog.jsonb_build_object(
                'X-AAP-Route-Admission', v_header_token
            )
        ));

    WHEN 'settle_control_attempt' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['control_id', 'capability', 'attempt_id', 'kind',
                          'status_code', 'reason']
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing control settlement';
        END IF;
        v_control_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'control_id');
        v_attempt_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'attempt_id');
        v_capability := v_args ->> 'capability';
        v_finish_kind := v_args ->> 'kind';
        IF pg_catalog.jsonb_typeof(v_args -> 'capability') IS DISTINCT FROM 'string'
           OR v_capability !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(v_args -> 'kind') IS DISTINCT FROM 'string'
           OR v_finish_kind NOT IN ('response', 'uncertain') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing control settlement';
        END IF;
        IF v_args -> 'status_code' <> 'null'::jsonb THEN
            v_status_code := aap_inference_routing_v1._routing_integer(
                v_args -> 'status_code', 100, 599
            )::integer;
        ELSE
            v_status_code := NULL;
        END IF;
        IF v_args -> 'reason' <> 'null'::jsonb THEN
            IF pg_catalog.jsonb_typeof(v_args -> 'reason') IS DISTINCT FROM 'string'
               OR (v_args ->> 'reason') !~ '^[a-z0-9_]{1,96}$' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing control settlement';
            END IF;
            v_reason := v_args ->> 'reason';
        ELSE
            v_reason := NULL;
        END IF;
        IF (v_finish_kind = 'response' AND (v_status_code IS NULL OR v_reason IS NOT NULL))
           OR (v_finish_kind = 'uncertain' AND v_status_code IS NOT NULL) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing control settlement';
        END IF;
        SELECT control.* INTO v_control
        FROM aap_inference_routing_v1.controls AS control
        WHERE control.id = v_control_id
        FOR UPDATE;
        IF NOT FOUND OR aap_inference_routing_v1._routing_control_capability(v_control_id)
            IS DISTINCT FROM v_capability THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END IF;
        SELECT attempt.* INTO v_attempt
        FROM aap_inference_routing_v1.control_attempts AS attempt
        WHERE attempt.id = v_attempt_id
          AND attempt.control_id = v_control_id
        FOR UPDATE;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END IF;
        IF v_attempt.state = 'reconciled' THEN
            RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        ELSIF v_attempt.state = 'response' THEN
            IF v_finish_kind <> 'response'
               OR v_attempt.status_code IS DISTINCT FROM v_status_code
               OR v_attempt.reason IS DISTINCT FROM v_reason THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing control attempt conflict';
            END IF;
        ELSIF v_attempt.state = 'uncertain' AND v_finish_kind = 'uncertain' THEN
            IF v_attempt.reason IS DISTINCT FROM v_reason THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'routing control attempt conflict';
            END IF;
        ELSE
            UPDATE aap_inference_routing_v1.control_attempts AS attempt
            SET state = v_finish_kind,
                status_code = CASE WHEN v_finish_kind = 'response' THEN v_status_code ELSE NULL END,
                reason = CASE WHEN v_finish_kind = 'uncertain' THEN v_reason ELSE NULL END,
                updated_at = pg_catalog.now()
            WHERE attempt.id = v_attempt_id;
        END IF;
        IF v_finish_kind = 'uncertain' THEN
            UPDATE aap_inference_routing_v1.controls AS control
            SET phase = 'uncertain', updated_at = pg_catalog.now()
            WHERE control.id = v_control_id
              AND control.phase <> 'completed';
        END IF;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'attempt', pg_catalog.jsonb_build_object(
                'id', v_attempt_id,
                'state', v_finish_kind,
                'status_code', v_status_code,
                'reason', v_reason
            )
        ));

    WHEN 'runtime_snapshot' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_args, ARRAY['registry_ids']
        ) OR pg_catalog.jsonb_typeof(v_args -> 'registry_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing runtime snapshot';
        END IF;
        IF pg_catalog.jsonb_array_length(v_args -> 'registry_ids') > 512 THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing runtime snapshot';
        END IF;
        v_registry_ids := ARRAY[]::uuid[];
        FOR v_json_element IN
            SELECT element.value
            FROM pg_catalog.jsonb_array_elements(v_args -> 'registry_ids') AS element(value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_json_element) IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing runtime registry';
            END IF;
            v_item := aap_inference_routing_v1._routing_uuid(v_json_element #>> '{}');
            IF v_item = ANY(v_registry_ids) THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'duplicate routing runtime registry';
            END IF;
            v_registry_ids := pg_catalog.array_append(v_registry_ids, v_item);
        END LOOP;

        v_secret := aap_inference_routing_v1._routing_secret();
        v_realm := aap_inference_routing_v1._routing_realm();
        v_now := pg_catalog.statement_timestamp();
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'registry_id', requested.registry_id,
                    'open_scopes', scope_counts.open_scopes,
                    'active_attempts', scope_counts.active_attempts,
                    'uncertain_attempts', scope_counts.uncertain_attempts,
                    'continuity', CASE
                        WHEN v_secret IS NOT NULL
                         AND state.realm IS NOT DISTINCT FROM v_realm
                         AND state.continuity = 'ready'
                        THEN 'ready' ELSE 'unverified' END,
                    'authority_epoch', CASE
                        WHEN v_secret IS NOT NULL
                         AND state.realm IS NOT DISTINCT FROM v_realm
                         AND state.continuity = 'ready'
                        THEN state.authority_epoch ELSE NULL END,
                    'backend_incarnation', CASE
                        WHEN v_secret IS NOT NULL
                         AND state.realm IS NOT DISTINCT FROM v_realm
                         AND state.continuity = 'ready'
                        THEN state.backend_incarnation ELSE NULL END,
                    'control_id', control.id
                ) ORDER BY requested.registry_id
            ),
            '[]'::jsonb
        )
        INTO v_result
        FROM pg_catalog.unnest(v_registry_ids) AS requested(registry_id)
        JOIN public.ml_backend_registry AS registry
          ON registry.id = requested.registry_id
        LEFT JOIN aap_inference_routing_v1.registry_state AS state
          ON state.registry_id = requested.registry_id
        LEFT JOIN LATERAL (
            SELECT
                pg_catalog.count(DISTINCT scope.id) FILTER (
                    WHERE scope.state IN ('active', 'uncertain')
                       OR (scope.state = 'reserved' AND scope.heartbeat_deadline > v_now)
                       OR attempt.state IN ('active', 'uncertain')
                ) AS open_scopes,
                pg_catalog.count(DISTINCT attempt.id) FILTER (
                    WHERE attempt.state = 'active'
                ) AS active_attempts,
                pg_catalog.count(DISTINCT attempt.id) FILTER (
                    WHERE attempt.state = 'uncertain'
                ) AS uncertain_attempts
            FROM aap_inference_routing_v1.scopes AS scope
            LEFT JOIN aap_inference_routing_v1.attempts AS attempt
              ON attempt.scope_id = scope.id
            WHERE scope.registry_id = requested.registry_id
        ) AS scope_counts ON TRUE
        LEFT JOIN aap_inference_routing_v1.controls AS control
          ON control.registry_id = requested.registry_id
         AND control.phase IN ('prepared', 'draining', 'uncertain');

        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'observed_at', pg_catalog.to_char(
                v_now AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
            ),
            'registries', v_result
        ));

    ELSE
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid routing action';
    END CASE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.routing_step(text, text)
    FROM PUBLIC;
