-- Durable video execution claims and route-intent binding.
-- Job locks are always acquired before the private execution row. Inference
-- reservation and this binding share one transaction; attempts are fenced by
-- the current bound generation.
CREATE TABLE aap_jobs_v1.video_execution_authority (
    singleton boolean PRIMARY KEY CHECK (singleton IS TRUE),
    secret bytea NOT NULL CHECK (pg_catalog.octet_length(secret) = 32),
    database_oid oid NOT NULL,
    database_name text NOT NULL,
    cluster_identifier bigint NOT NULL
);
-- aap-statement-break
INSERT INTO aap_jobs_v1.video_execution_authority (
    singleton, secret, database_oid, database_name, cluster_identifier
)
SELECT true, public.gen_random_bytes(32), database.oid, pg_catalog.current_database(),
       control.system_identifier
FROM pg_catalog.pg_database AS database
CROSS JOIN LATERAL pg_catalog.pg_control_system() AS control
WHERE database.datname = pg_catalog.current_database();
-- aap-statement-break
CREATE TABLE aap_jobs_v1.video_executions (
    job_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    worker_id uuid NOT NULL,
    generation bigint NOT NULL CHECK (generation >= 1),
    lease_token_hash bytea NOT NULL CHECK (pg_catalog.octet_length(lease_token_hash) = 32),
    lease_expires_at timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('ready', 'recovery_required')),
    route_request_id uuid,
    realm text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE TABLE aap_jobs_v1.video_execution_route_intents (
    request_id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES aap_jobs_v1.video_executions(job_id) ON DELETE RESTRICT,
    run_id uuid NOT NULL,
    generation bigint NOT NULL CHECK (generation >= 1),
    command_sha256 text NOT NULL CHECK (command_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'),
    command_ciphertext bytea NOT NULL CHECK (pg_catalog.octet_length(command_ciphertext) >= 64),
    scope_id uuid NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN ('prepared', 'bound')),
    realm text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (job_id, request_id)
);
-- aap-statement-break
CREATE UNIQUE INDEX uq_aap_video_execution_prepared_generation
    ON aap_jobs_v1.video_execution_route_intents(job_id, generation)
    WHERE state = 'prepared';
-- aap-statement-break
CREATE INDEX ix_aap_video_execution_route_job
    ON aap_jobs_v1.video_execution_route_intents(job_id, generation DESC, created_at DESC);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.video_execution_authority,
    aap_jobs_v1.video_executions,
    aap_jobs_v1.video_execution_route_intents FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_execution_reply(
    p_data jsonb,
    p_reason text DEFAULT NULL
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
    SELECT CASE WHEN p_reason IS NULL THEN
        pg_catalog.jsonb_build_object(
            'schema', 'aap-video-execution-data/v1', 'status', 'ok'
        ) || COALESCE(p_data, '{}'::jsonb)
    ELSE
        pg_catalog.jsonb_build_object(
            'schema', 'aap-video-execution-data/v1',
            'status', 'rejected',
            'reason', p_reason
        )
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_execution_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_execution_uuid(p_value jsonb)
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
    v_text text;
    v_uuid uuid;
BEGIN
    IF pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid video execution request';
    END IF;
    v_text := p_value #>> '{}';
    BEGIN
        v_uuid := v_text::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid video execution request';
    END;
    IF v_uuid::text IS DISTINCT FROM v_text THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid video execution request';
    END IF;
    RETURN v_uuid;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_execution_uuid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_execution_exact_keys(
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
    SELECT CASE WHEN pg_catalog.jsonb_typeof(p_value) = 'object' THEN
        (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_value))
            = pg_catalog.cardinality(p_keys)
        AND p_value ?& p_keys
    ELSE false END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_execution_exact_keys(jsonb, text[]) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_execution_realm()
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
    SELECT database.oid::text || ':' || database.datname || ':' || control.system_identifier::text
    FROM pg_catalog.pg_database AS database
    CROSS JOIN LATERAL pg_catalog.pg_control_system() AS control
    WHERE database.datname = pg_catalog.current_database()
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_execution_realm() FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_execution_secret()
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
    SELECT authority.secret
    FROM aap_jobs_v1.video_execution_authority AS authority
    JOIN pg_catalog.pg_database AS database
      ON database.oid = authority.database_oid
     AND database.datname = authority.database_name
    JOIN LATERAL pg_catalog.pg_control_system() AS control
      ON control.system_identifier = authority.cluster_identifier
    WHERE authority.singleton IS TRUE
      AND database.datname = pg_catalog.current_database()
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_execution_secret() FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_execution_claim_view(
    p_job_id uuid,
    p_token uuid
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
        'job_id', execution.job_id,
        'run_id', execution.run_id,
        'worker_id', execution.worker_id,
        'generation', execution.generation,
        'lease_token', p_token,
        'lease_expires_at', pg_catalog.to_char(
            execution.lease_expires_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
        ),
        'state', execution.state
    )
    FROM aap_jobs_v1.video_executions AS execution
    WHERE execution.job_id = p_job_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_execution_claim_view(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_execution_route_step(
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
SET lock_timeout = '250ms'
AS $function$
DECLARE
    v_args jsonb;
    v_job_id uuid;
    v_run_id uuid;
    v_worker_id uuid;
    v_token uuid;
    v_generation bigint;
    v_request_id uuid;
    v_scope_id uuid;
    v_digest text;
    v_command jsonb;
    v_retired_prior_scope_id uuid;
    v_job record;
    v_execution aap_jobs_v1.video_executions%ROWTYPE;
    v_intent aap_jobs_v1.video_execution_route_intents%ROWTYPE;
    v_input record;
    v_body jsonb;
    v_embedded jsonb;
    v_expected_keys text[];
    v_state text;
    v_latest record;
    v_secret bytea;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_job_missing boolean := false;
BEGIN
    IF p_action NOT IN ('policy', 'prepare', 'reserve_check', 'bind', 'status', 'record_check')
       OR p_arguments IS NULL OR pg_catalog.octet_length(p_arguments) > 2097152 THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'invalid_request');
    END IF;
    BEGIN
        v_args := p_arguments::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'invalid_request');
    END;
    IF pg_catalog.jsonb_typeof(v_args) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'invalid_request');
    END IF;
    IF p_action = 'policy' THEN
        IF NOT aap_jobs_v1._video_execution_exact_keys(v_args, ARRAY[]::text[]) THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'invalid_request');
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'ok', true,
            'ready', aap_jobs_v1._video_execution_secret() IS NOT NULL
        );
    END IF;
    IF p_action = 'prepare' THEN
        v_expected_keys := ARRAY[
            'job_id','run_id','worker_id','generation','lease_token','request_id',
            'command','command_sha256','scope_id','retired_prior_scope_id'
        ];
    ELSIF p_action = 'status' THEN
        v_expected_keys := ARRAY['job_id','run_id','worker_id','generation','lease_token'];
    ELSIF p_action = 'record_check' THEN
        v_expected_keys := ARRAY[
            'job_id','run_id','worker_id','generation','lease_token',
            'request_id','command_sha256'
        ];
    ELSE
        v_expected_keys := ARRAY[
            'job_id','run_id','worker_id','generation','lease_token',
            'request_id','command_sha256','scope_id'
        ];
    END IF;
    IF NOT aap_jobs_v1._video_execution_exact_keys(v_args, v_expected_keys) THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'invalid_request');
    END IF;
    BEGIN
        v_job_id := aap_jobs_v1._video_execution_uuid(v_args -> 'job_id');
        v_run_id := aap_jobs_v1._video_execution_uuid(v_args -> 'run_id');
        v_worker_id := aap_jobs_v1._video_execution_uuid(v_args -> 'worker_id');
        v_token := aap_jobs_v1._video_execution_uuid(v_args -> 'lease_token');
        v_generation := (v_args ->> 'generation')::bigint;
        IF p_action <> 'status' THEN
            v_request_id := aap_jobs_v1._video_execution_uuid(v_args -> 'request_id');
            IF p_action <> 'record_check' THEN
                v_scope_id := aap_jobs_v1._video_execution_uuid(v_args -> 'scope_id');
            END IF;
            v_digest := v_args ->> 'command_sha256';
            IF pg_catalog.jsonb_typeof(v_args -> 'command_sha256') IS DISTINCT FROM 'string'
               OR v_digest !~ '^[0-9a-f]{64}$' THEN
                RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'invalid_request');
            END IF;
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'invalid_request');
    END;
    IF pg_catalog.jsonb_typeof(v_args -> 'generation') IS DISTINCT FROM 'number'
       OR v_generation < 1 OR (v_args ->> 'generation') !~ '^[1-9][0-9]*$' THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'invalid_request');
    END IF;
    v_secret := aap_jobs_v1._video_execution_secret();
    IF v_secret IS NULL THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'realm_mismatch');
    END IF;

    -- All claim/intent paths follow Labeling job -> Jobs execution lock order.
    SELECT job.id, job.status, job.cancel_requested_at
    INTO v_job
    FROM public.video_tracker_jobs AS job
    WHERE job.id = v_job_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        v_job_missing := true;
        IF p_action <> 'status' THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'job_missing');
        END IF;
    END IF;
    SELECT execution.* INTO v_execution
    FROM aap_jobs_v1.video_executions AS execution
    WHERE execution.job_id = v_job_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'lease_lost');
    END IF;
    IF v_execution.realm IS DISTINCT FROM aap_jobs_v1._video_execution_realm()
       OR v_execution.realm IS DISTINCT FROM (
            SELECT authority.database_oid::text || ':' || authority.database_name
                   || ':' || authority.cluster_identifier::text
            FROM aap_jobs_v1.video_execution_authority AS authority
            WHERE authority.singleton IS TRUE
       ) THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'realm_mismatch');
    END IF;
    IF v_execution.run_id IS DISTINCT FROM v_run_id
       OR v_execution.worker_id IS DISTINCT FROM v_worker_id
       OR v_execution.generation IS DISTINCT FROM v_generation
       OR v_execution.lease_token_hash IS DISTINCT FROM public.hmac(
            pg_catalog.convert_to(v_token::text, 'UTF8'), v_secret, 'sha256'
       ) THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'lease_lost');
    END IF;
    IF v_execution.lease_expires_at <= v_now THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'lease_lost');
    END IF;
    IF v_job_missing THEN
        IF v_execution.state <> 'recovery_required' THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'job_missing');
        END IF;
    ELSE
        IF v_job.status NOT IN ('queued', 'running') AND p_action <> 'status' THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'job_not_active');
        END IF;
        IF v_job.status = 'cancelled' AND p_action <> 'status' THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'job_cancelled');
        END IF;
    END IF;

    IF p_action = 'status' THEN
        IF v_execution.route_request_id IS NULL THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', true, 'intent', NULL, 'command', NULL
            );
        END IF;
        SELECT intent.* INTO v_intent
        FROM aap_jobs_v1.video_execution_route_intents AS intent
        WHERE intent.request_id = v_execution.route_request_id
          AND intent.job_id = v_job_id;
        IF NOT FOUND OR v_intent.realm IS DISTINCT FROM v_execution.realm
           OR v_intent.run_id IS DISTINCT FROM v_execution.run_id
           OR v_intent.generation > v_execution.generation THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'realm_mismatch');
        END IF;
        v_command := public.pgp_sym_decrypt(
            v_intent.command_ciphertext, pg_catalog.encode(v_secret,'hex')
        )::jsonb;
        RETURN pg_catalog.jsonb_build_object(
            'ok', true,
            'command', v_command,
            'intent', pg_catalog.jsonb_build_object(
            'job_id', v_intent.job_id,
            'run_id', v_intent.run_id,
            'generation', v_intent.generation,
            'request_id', v_intent.request_id,
            'command_sha256', v_intent.command_sha256,
            'scope_id', v_intent.scope_id,
            'state', v_intent.state
        ));
    END IF;
    IF NOT v_job_missing THEN
        IF v_job.cancel_requested_at IS NOT NULL THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'job_cancelled');
        END IF;
    END IF;

    IF p_action = 'prepare' THEN
        v_command := v_args -> 'command';
        v_retired_prior_scope_id := CASE
            WHEN v_args -> 'retired_prior_scope_id' = 'null'::jsonb THEN NULL
            ELSE aap_jobs_v1._video_execution_uuid(v_args -> 'retired_prior_scope_id')
        END;
        IF pg_catalog.jsonb_typeof(v_command) IS DISTINCT FROM 'object'
           OR pg_catalog.octet_length(v_command::text) > 65536
           OR NOT aap_jobs_v1._video_execution_exact_keys(
                v_command, ARRAY['schema','request_id','operation','body']
           )
           OR v_command ->> 'schema' IS DISTINCT FROM 'aap-inference-routing/v1'
           OR v_command ->> 'operation' IS DISTINCT FROM 'video_acquire'
           OR pg_catalog.jsonb_typeof(v_command -> 'body') IS DISTINCT FROM 'object'
           OR NOT aap_jobs_v1._video_execution_exact_keys(
                v_command -> 'body', ARRAY[
                    'pool_id','registry_id','project_id','mode','required_instance_id',
                    'owner','operation','execution'
                ]
           )
           OR pg_catalog.jsonb_typeof(v_command #> '{body,execution}') IS DISTINCT FROM 'object'
           OR NOT aap_jobs_v1._video_execution_exact_keys(
                v_command #> '{body,execution}',
                ARRAY['job_id','run_id','worker_id','generation','lease_token']
           )
           OR v_command ->> 'request_id' IS DISTINCT FROM v_request_id::text
           OR aap_jobs_v1._video_execution_uuid(v_command #> '{body,execution,job_id}')
                IS DISTINCT FROM v_job_id
           OR aap_jobs_v1._video_execution_uuid(v_command #> '{body,execution,run_id}')
                IS DISTINCT FROM v_run_id
           OR aap_jobs_v1._video_execution_uuid(v_command #> '{body,execution,worker_id}')
                IS DISTINCT FROM v_worker_id
           OR (v_command #>> '{body,execution,generation}')::bigint IS DISTINCT FROM v_generation
           OR aap_jobs_v1._video_execution_uuid(v_command #> '{body,execution,lease_token}')
                IS DISTINCT FROM v_token
           OR v_command #>> '{body,owner}' IS DISTINCT FROM 'tracker:' || v_job_id::text
           OR v_command #>> '{body,operation}' IS DISTINCT FROM 'tracker'
           OR v_command #>> '{body,project_id}' IS DISTINCT FROM (
                SELECT input_scope.project_id::text
                FROM public.video_tracker_input_scopes AS input_scope
                WHERE input_scope.tracker_job_id = v_job_id
                  AND input_scope.prepare_error IS NULL
           )
           OR pg_catalog.encode(public.digest(pg_catalog.convert_to(
                aap_inference_routing_v1._video_execution_canonical_json(
                    v_command - 'request_id'
                ), 'UTF8'), 'sha256'), 'hex') IS DISTINCT FROM v_digest THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'invalid_command');
        END IF;
        IF v_job.cancel_requested_at IS NOT NULL THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'job_cancelled');
        END IF;

        SELECT intent.* INTO v_intent
        FROM aap_jobs_v1.video_execution_route_intents AS intent
        WHERE intent.request_id = v_request_id;
        IF FOUND THEN
            IF v_intent.job_id IS DISTINCT FROM v_job_id
               OR v_intent.run_id IS DISTINCT FROM v_run_id
               OR v_intent.generation IS DISTINCT FROM v_generation
               OR v_intent.command_sha256 IS DISTINCT FROM v_digest
               OR public.pgp_sym_decrypt(
                    v_intent.command_ciphertext, pg_catalog.encode(v_secret,'hex')
                  ) IS DISTINCT FROM v_command::text
               OR v_intent.scope_id IS DISTINCT FROM v_scope_id
               OR v_intent.realm IS DISTINCT FROM v_execution.realm THEN
                RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'intent_conflict');
            END IF;
            UPDATE aap_jobs_v1.video_executions
            SET route_request_id = v_request_id, updated_at = v_now
            WHERE job_id = v_job_id;
            RETURN pg_catalog.jsonb_build_object(
                'ok', true,
                'command', v_command,
                'intent', pg_catalog.jsonb_build_object(
                'job_id', v_intent.job_id, 'run_id', v_intent.run_id,
                'generation', v_intent.generation, 'request_id', v_intent.request_id,
                'command_sha256', v_intent.command_sha256, 'scope_id', v_intent.scope_id,
                'state', v_intent.state
            ));
        END IF;

        IF v_execution.route_request_id IS NOT NULL THEN
            SELECT intent.* INTO v_latest
            FROM aap_jobs_v1.video_execution_route_intents AS intent
            WHERE intent.request_id = v_execution.route_request_id
              AND intent.job_id = v_job_id;
            IF FOUND AND v_latest.state = 'bound' THEN
                IF v_retired_prior_scope_id IS DISTINCT FROM v_latest.scope_id THEN
                    RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'recovery_required');
                END IF;
            ELSIF FOUND AND v_latest.state = 'prepared'
                  AND v_latest.generation = v_generation
                  AND v_latest.request_id IS DISTINCT FROM v_request_id THEN
                RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'intent_conflict');
            ELSIF v_retired_prior_scope_id IS NOT NULL THEN
                RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'intent_conflict');
            END IF;
        ELSIF v_retired_prior_scope_id IS NOT NULL THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'intent_conflict');
        END IF;
        INSERT INTO aap_jobs_v1.video_execution_route_intents (
            request_id, job_id, run_id, generation, command_sha256,
            command_ciphertext, scope_id, state, realm
        ) VALUES (
            v_request_id, v_job_id, v_run_id, v_generation, v_digest,
            public.pgp_sym_encrypt(
                v_command::text, pg_catalog.encode(v_secret,'hex'), 'cipher-algo=aes256'
            ), v_scope_id, 'prepared', v_execution.realm
        );
        UPDATE aap_jobs_v1.video_executions
        SET route_request_id = v_request_id, updated_at = v_now
        WHERE job_id = v_job_id;
        RETURN pg_catalog.jsonb_build_object(
            'ok', true,
            'command', v_command,
            'intent', pg_catalog.jsonb_build_object(
            'job_id', v_job_id, 'run_id', v_run_id, 'generation', v_generation,
            'request_id', v_request_id, 'command_sha256', v_digest,
            'scope_id', v_scope_id, 'state', 'prepared'
        ));
    END IF;

    SELECT intent.* INTO v_intent
    FROM aap_jobs_v1.video_execution_route_intents AS intent
    WHERE intent.request_id = v_request_id
      AND intent.job_id = v_job_id
    FOR UPDATE;
    IF NOT FOUND OR v_intent.run_id IS DISTINCT FROM v_run_id
       OR v_intent.generation IS DISTINCT FROM v_generation
       OR v_intent.command_sha256 IS DISTINCT FROM v_digest
       OR (p_action <> 'record_check' AND v_intent.scope_id IS DISTINCT FROM v_scope_id)
       OR v_intent.realm IS DISTINCT FROM v_execution.realm THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'scope_mismatch');
    END IF;
    IF v_execution.route_request_id IS DISTINCT FROM v_request_id THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'scope_mismatch');
    END IF;
    v_command := public.pgp_sym_decrypt(
        v_intent.command_ciphertext, pg_catalog.encode(v_secret,'hex')
    )::jsonb;
    IF p_action = 'bind' THEN
        IF v_intent.state NOT IN ('prepared','bound') THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'scope_mismatch');
        END IF;
        UPDATE aap_jobs_v1.video_execution_route_intents
        SET state = 'bound', updated_at = v_now
        WHERE request_id = v_request_id AND job_id = v_job_id;
        UPDATE aap_jobs_v1.video_executions
        SET route_request_id = v_request_id, state = 'ready', updated_at = v_now
        WHERE job_id = v_job_id;
        RETURN pg_catalog.jsonb_build_object(
            'ok', true,
            'command', v_command,
            'intent', pg_catalog.jsonb_build_object(
            'job_id', v_job_id, 'run_id', v_run_id, 'generation', v_generation,
            'request_id', v_request_id, 'command_sha256', v_digest,
            'scope_id', v_scope_id, 'state', 'bound'
        ));
    END IF;
    IF p_action = 'record_check' THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', true,
            'command', v_command,
            'intent', pg_catalog.jsonb_build_object(
                'job_id', v_job_id, 'run_id', v_intent.run_id,
                'generation', v_intent.generation, 'request_id', v_request_id,
                'command_sha256', v_intent.command_sha256,
                'scope_id', v_intent.scope_id, 'state', v_intent.state
            )
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'ok', true,
        'command', v_command,
        'intent', pg_catalog.jsonb_build_object(
            'job_id', v_job_id, 'run_id', v_intent.run_id,
            'generation', v_intent.generation, 'request_id', v_request_id,
            'command_sha256', v_intent.command_sha256,
            'scope_id', v_intent.scope_id, 'state', v_intent.state
        )
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'execution_busy');
    WHEN invalid_text_representation OR invalid_parameter_value THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'reason', 'invalid_request');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_execution_route_step(text, text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.video_execution_step(
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
SET lock_timeout = '250ms'
AS $function$
DECLARE
    v_args jsonb;
    v_job_id uuid;
    v_run_id uuid;
    v_worker_id uuid;
    v_token uuid;
    v_generation bigint;
    v_seconds integer;
    v_purpose text;
    v_job record;
    v_execution aap_jobs_v1.video_executions%ROWTYPE;
    v_secret bytea;
    v_realm text;
    v_token_hash bytea;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_expires timestamptz;
    v_state text;
    v_bound boolean := false;
    v_route_bound boolean := false;
    v_bound_state text;
    v_bound_run_id uuid;
    v_cancel_cleanup boolean := false;
    v_terminal_cleanup boolean := false;
    v_job_missing boolean := false;
    v_keys text[];
BEGIN
    IF p_arguments IS NULL OR pg_catalog.octet_length(p_arguments) > 65536 THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        v_args := p_arguments::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'invalid_request');
    END;
    IF pg_catalog.jsonb_typeof(v_args) IS DISTINCT FROM 'object' THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'invalid_request');
    END IF;
    IF p_action = 'policy' THEN
        IF NOT aap_jobs_v1._video_execution_exact_keys(v_args, ARRAY[]::text[]) THEN
            RETURN aap_jobs_v1._video_execution_reply(NULL, 'invalid_request');
        END IF;
        RETURN aap_jobs_v1._video_execution_reply(pg_catalog.jsonb_build_object(
            'ready', aap_jobs_v1._video_execution_secret() IS NOT NULL,
            'functions', pg_catalog.jsonb_build_array(
                'aap_jobs_v1.video_execution_step(text,text)'
            )
        ));
    END IF;
    IF p_action = 'claim' THEN
        v_keys := ARRAY['job_id','worker_id','lease_seconds'];
    ELSIF p_action = 'renew' THEN
        v_keys := ARRAY[
            'job_id','run_id','worker_id','generation','lease_token','lease_seconds'
        ];
    ELSIF p_action = 'guard' THEN
        v_keys := ARRAY['job_id','run_id','worker_id','generation','lease_token','purpose'];
    ELSIF p_action = 'release' THEN
        v_keys := ARRAY['job_id','run_id','worker_id','generation','lease_token'];
    ELSE
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'invalid_request');
    END IF;
    IF NOT aap_jobs_v1._video_execution_exact_keys(v_args, v_keys) THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        v_job_id := aap_jobs_v1._video_execution_uuid(v_args -> 'job_id');
        v_worker_id := aap_jobs_v1._video_execution_uuid(v_args -> 'worker_id');
        IF p_action <> 'claim' THEN
            v_run_id := aap_jobs_v1._video_execution_uuid(v_args -> 'run_id');
            v_token := aap_jobs_v1._video_execution_uuid(v_args -> 'lease_token');
            v_generation := (v_args ->> 'generation')::bigint;
            IF pg_catalog.jsonb_typeof(v_args -> 'generation') IS DISTINCT FROM 'number'
               OR (v_args ->> 'generation') !~ '^[1-9][0-9]*$'
               OR v_generation < 1 THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'invalid_request');
            END IF;
        END IF;
        IF p_action IN ('claim', 'renew') THEN
            IF pg_catalog.jsonb_typeof(v_args -> 'lease_seconds') IS DISTINCT FROM 'number'
               OR (v_args ->> 'lease_seconds') !~ '^[0-9]+$' THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'invalid_request');
            END IF;
            v_seconds := (v_args ->> 'lease_seconds')::integer;
            IF v_seconds NOT BETWEEN 5 AND 300 THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'invalid_request');
            END IF;
        ELSIF p_action = 'guard' THEN
            IF pg_catalog.jsonb_typeof(v_args -> 'purpose') IS DISTINCT FROM 'string' THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'invalid_request');
            END IF;
            v_purpose := v_args ->> 'purpose';
            IF v_purpose NOT IN ('start','progress','mask_root','stage','cancel','fail') THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'invalid_request');
            END IF;
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'invalid_request');
    END;
    v_secret := aap_jobs_v1._video_execution_secret();
    v_realm := aap_jobs_v1._video_execution_realm();
    IF v_secret IS NULL OR v_realm IS NULL THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'realm_mismatch');
    END IF;

    IF p_action = 'claim' THEN
        SELECT job.id, job.status, job.cancel_requested_at
        INTO v_job
        FROM public.video_tracker_jobs AS job
        WHERE job.id = v_job_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            -- The public job may have been deleted after a live execution was
            -- accepted. Rotate an expired proof into cleanup-only recovery so
            -- its exact bound scope can still be retired; no business guard
            -- accepts this state and no route prepare can proceed without Job.
            SELECT execution.* INTO v_execution
            FROM aap_jobs_v1.video_executions AS execution
            WHERE execution.job_id = v_job_id
            FOR UPDATE NOWAIT;
            IF NOT FOUND THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_missing');
            END IF;
            IF v_execution.realm IS DISTINCT FROM v_realm THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'realm_mismatch');
            END IF;
            IF v_execution.lease_expires_at > v_now THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'execution_busy');
            END IF;
            IF v_execution.generation >= 9223372036854775807 THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
            END IF;
            v_run_id := v_execution.run_id;
            v_token := public.gen_random_uuid();
            v_generation := v_execution.generation + 1;
            v_expires := v_now + v_seconds * interval '1 second';
            UPDATE aap_jobs_v1.video_executions AS execution
            SET worker_id = v_worker_id,
                generation = v_generation,
                lease_token_hash = public.hmac(
                    pg_catalog.convert_to(v_token::text, 'UTF8'), v_secret, 'sha256'
                ),
                lease_expires_at = v_expires,
                state = 'recovery_required',
                updated_at = v_now
            WHERE execution.job_id = v_job_id;
            RETURN aap_jobs_v1._video_execution_reply(pg_catalog.jsonb_build_object(
                'execution', pg_catalog.jsonb_build_object(
                    'job_id', v_job_id, 'run_id', v_run_id, 'worker_id', v_worker_id,
                    'generation', v_generation, 'lease_token', v_token,
                    'lease_expires_at', pg_catalog.to_char(
                        v_expires AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                    ), 'state', 'recovery_required'
                )
            ));
        END IF;
        v_cancel_cleanup := v_job.status = 'cancelled'
            OR v_job.cancel_requested_at IS NOT NULL;
        IF v_job.status NOT IN (
            'queued','running','cancelled','completed','failed','pending_review',
            'partially_reviewed','accepted','discarded'
        ) THEN
            RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
        END IF;
        v_terminal_cleanup := v_job.status IN (
            'completed','failed','pending_review','partially_reviewed','accepted','discarded'
        );
        IF NOT EXISTS (
            SELECT 1 FROM public.video_tracker_input_scopes AS input_scope
            WHERE input_scope.tracker_job_id = v_job_id
              AND input_scope.prepare_error IS NULL
        ) THEN
            RETURN aap_jobs_v1._video_execution_reply(NULL, 'scope_mismatch');
        END IF;
        SELECT execution.* INTO v_execution
        FROM aap_jobs_v1.video_executions AS execution
        WHERE execution.job_id = v_job_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            IF v_job.status = 'running' THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'legacy_execution_unbound');
            END IF;
            IF v_cancel_cleanup THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_cancelled');
            END IF;
            IF v_terminal_cleanup THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
            END IF;
            v_run_id := public.gen_random_uuid();
            v_token := public.gen_random_uuid();
            v_generation := 1;
            v_expires := v_now + v_seconds * interval '1 second';
            INSERT INTO aap_jobs_v1.video_executions (
                job_id, run_id, worker_id, generation, lease_token_hash,
                lease_expires_at, state, route_request_id, realm
            ) VALUES (
                v_job_id, v_run_id, v_worker_id, v_generation,
                public.hmac(pg_catalog.convert_to(v_token::text, 'UTF8'), v_secret, 'sha256'),
                v_expires, 'ready', NULL, v_realm
            );
            RETURN aap_jobs_v1._video_execution_reply(pg_catalog.jsonb_build_object(
                'execution', pg_catalog.jsonb_build_object(
                    'job_id', v_job_id, 'run_id', v_run_id, 'worker_id', v_worker_id,
                    'generation', v_generation, 'lease_token', v_token,
                    'lease_expires_at', pg_catalog.to_char(
                        v_expires AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                    ), 'state', 'ready'
                )
            ));
        END IF;
        IF v_execution.realm IS DISTINCT FROM v_realm THEN
            RETURN aap_jobs_v1._video_execution_reply(NULL, 'realm_mismatch');
        END IF;
        IF v_execution.lease_expires_at > v_now THEN
            RETURN aap_jobs_v1._video_execution_reply(NULL, 'execution_busy');
        END IF;
        IF v_execution.generation >= 9223372036854775807 THEN
            RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
        END IF;
        IF v_execution.route_request_id IS NOT NULL THEN
            SELECT intent.state, intent.run_id INTO v_bound_state, v_bound_run_id
            FROM aap_jobs_v1.video_execution_route_intents AS intent
            WHERE intent.request_id = v_execution.route_request_id
              AND intent.job_id = v_job_id;
            v_bound := NOT FOUND OR v_bound_state = 'bound';
            v_route_bound := FOUND
                AND v_bound_state = 'bound'
                AND v_bound_run_id = v_execution.run_id;
        END IF;
        IF v_terminal_cleanup AND NOT v_route_bound THEN
            RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
        END IF;
        v_run_id := v_execution.run_id;
        v_token := public.gen_random_uuid();
        v_generation := v_execution.generation + 1;
        v_expires := v_now + v_seconds * interval '1 second';
        v_state := CASE
            WHEN v_bound OR v_cancel_cleanup THEN 'recovery_required'
            ELSE 'ready'
        END;
        UPDATE aap_jobs_v1.video_executions AS execution
        SET run_id = v_run_id,
            worker_id = v_worker_id,
            generation = v_generation,
            lease_token_hash = public.hmac(
                pg_catalog.convert_to(v_token::text, 'UTF8'), v_secret, 'sha256'
            ),
            lease_expires_at = v_expires,
            state = v_state,
            realm = v_realm,
            updated_at = v_now
        WHERE execution.job_id = v_job_id;
        RETURN aap_jobs_v1._video_execution_reply(pg_catalog.jsonb_build_object(
            'execution', pg_catalog.jsonb_build_object(
                'job_id', v_job_id, 'run_id', v_run_id, 'worker_id', v_worker_id,
                'generation', v_generation, 'lease_token', v_token,
                'lease_expires_at', pg_catalog.to_char(
                    v_expires AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                ), 'state', v_state
            )
        ));
    END IF;

    -- Non-claim actions also lock canonical Labeling state first.
    SELECT job.id, job.status, job.cancel_requested_at
    INTO v_job
    FROM public.video_tracker_jobs AS job
    WHERE job.id = v_job_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        v_job_missing := true;
        IF p_action <> 'renew' THEN
            RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_missing');
        END IF;
    END IF;
    SELECT execution.* INTO v_execution
    FROM aap_jobs_v1.video_executions AS execution
    WHERE execution.job_id = v_job_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'lease_lost');
    END IF;
    IF v_execution.realm IS DISTINCT FROM v_realm THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'realm_mismatch');
    END IF;
    v_token_hash := public.hmac(
        pg_catalog.convert_to(v_token::text, 'UTF8'), v_secret, 'sha256'
    );
    IF v_execution.run_id IS DISTINCT FROM v_run_id
       OR v_execution.worker_id IS DISTINCT FROM v_worker_id
       OR v_execution.generation IS DISTINCT FROM v_generation
       OR v_execution.lease_token_hash IS DISTINCT FROM v_token_hash THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'lease_lost');
    END IF;

    IF p_action = 'release' THEN
        IF EXISTS (
            SELECT 1 FROM aap_jobs_v1.video_execution_route_intents AS intent
            WHERE intent.job_id = v_job_id
              AND intent.run_id = v_run_id
              AND (
                  intent.state <> 'prepared'
                  OR NOT aap_inference_routing_v1._video_execution_no_scope_rejection(
                      intent.scope_id, intent.request_id, intent.command_sha256
                  )
              )
        ) OR (
            v_execution.route_request_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM aap_jobs_v1.video_execution_route_intents AS intent
                WHERE intent.request_id = v_execution.route_request_id
                  AND intent.job_id = v_job_id AND intent.run_id = v_run_id
            )
        ) THEN
            RETURN aap_jobs_v1._video_execution_reply(NULL, 'recovery_required');
        END IF;
        UPDATE aap_jobs_v1.video_executions
        SET lease_expires_at = LEAST(lease_expires_at, v_now),
            route_request_id = NULL, updated_at = v_now
        WHERE job_id = v_job_id;
        RETURN aap_jobs_v1._video_execution_reply(
            pg_catalog.jsonb_build_object('released', true)
        );
    END IF;

    IF p_action = 'renew' THEN
        IF v_execution.lease_expires_at <= v_now THEN
            RETURN aap_jobs_v1._video_execution_reply(NULL, 'lease_lost');
        END IF;
        IF v_job_missing THEN
            IF v_execution.state <> 'recovery_required' THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_missing');
            END IF;
        ELSE
            IF v_job.status IN (
                'completed','failed','pending_review','partially_reviewed','accepted','discarded'
            ) AND v_execution.state <> 'recovery_required' THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
            END IF;
            IF v_job.status NOT IN (
                'queued','running','cancelled','completed','failed','pending_review',
                'partially_reviewed','accepted','discarded'
            ) THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
            END IF;
        END IF;
        v_expires := v_now + v_seconds * interval '1 second';
        UPDATE aap_jobs_v1.video_executions
        SET lease_expires_at = v_expires, updated_at = v_now
        WHERE job_id = v_job_id;
        RETURN aap_jobs_v1._video_execution_reply(pg_catalog.jsonb_build_object(
            'execution', pg_catalog.jsonb_build_object(
                'job_id', v_job_id, 'run_id', v_run_id, 'worker_id', v_worker_id,
                'generation', v_generation, 'lease_token', v_token,
                'lease_expires_at', pg_catalog.to_char(
                    v_expires AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                ), 'state', v_execution.state
            )
        ));
    END IF;

    IF v_execution.lease_expires_at <= v_now THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'lease_lost');
    END IF;
    IF v_job.status IN ('completed','failed','pending_review','partially_reviewed',
                        'accepted','discarded') THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
    END IF;
    IF v_job.status = 'cancelled' AND v_purpose NOT IN ('progress','stage','cancel','fail') THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_cancelled');
    END IF;
    IF v_job.cancel_requested_at IS NOT NULL
       AND v_purpose IN ('start','mask_root') THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_cancelled');
    END IF;
    IF v_execution.state = 'recovery_required'
       AND v_purpose NOT IN ('cancel','fail') THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'recovery_required');
    END IF;
    IF v_purpose = 'start' AND v_job.status NOT IN ('queued','running') THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
    ELSIF v_purpose = 'progress' AND v_job.status NOT IN ('running','cancelled') THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
    ELSIF v_purpose = 'mask_root' AND v_job.status <> 'running' THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
    ELSIF v_purpose = 'stage' AND v_job.status NOT IN ('running','cancelled') THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
    ELSIF v_purpose IN ('cancel','fail') AND v_job.status NOT IN ('queued','running','cancelled') THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
    END IF;
    RETURN aap_jobs_v1._video_execution_reply(pg_catalog.jsonb_build_object('guarded', true));
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._video_execution_reply(NULL, 'execution_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.video_execution_step(text, text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_execution_scope_is_current(
    p_scope_id uuid,
    p_allow_cancelled boolean
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
    v_job_id uuid;
    v_run_id uuid;
    v_generation bigint;
    v_job record;
    v_execution aap_jobs_v1.video_executions%ROWTYPE;
    v_intent aap_jobs_v1.video_execution_route_intents%ROWTYPE;
BEGIN
    SELECT intent.job_id, intent.run_id, intent.generation
    INTO v_job_id, v_run_id, v_generation
    FROM aap_jobs_v1.video_execution_route_intents AS intent
    WHERE intent.scope_id = p_scope_id AND intent.state = 'bound';
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    SELECT job.id, job.status, job.cancel_requested_at
    INTO v_job FROM public.video_tracker_jobs AS job
    WHERE job.id = v_job_id FOR KEY SHARE NOWAIT;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF p_allow_cancelled IS TRUE THEN
        IF v_job.status NOT IN ('running','cancelled') THEN
            RETURN false;
        END IF;
    ELSIF v_job.status <> 'running' OR v_job.cancel_requested_at IS NOT NULL THEN
        RETURN false;
    END IF;
    SELECT execution.* INTO v_execution
    FROM aap_jobs_v1.video_executions AS execution
    WHERE execution.job_id = v_job_id FOR KEY SHARE NOWAIT;
    IF NOT FOUND OR v_execution.run_id IS DISTINCT FROM v_run_id
       OR v_execution.generation IS DISTINCT FROM v_generation
       OR v_execution.lease_expires_at <= pg_catalog.clock_timestamp()
       OR v_execution.state <> 'ready'
       OR v_execution.realm IS DISTINCT FROM aap_jobs_v1._video_execution_realm()
       OR v_execution.route_request_id IS NULL THEN
        RETURN false;
    END IF;
    SELECT intent.* INTO v_intent
    FROM aap_jobs_v1.video_execution_route_intents AS intent
    WHERE intent.request_id = v_execution.route_request_id
      AND intent.scope_id = p_scope_id AND intent.job_id = v_job_id
      AND intent.run_id = v_run_id AND intent.generation = v_generation
      AND intent.state = 'bound' AND intent.realm = v_execution.realm;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_execution_scope_is_current(uuid, boolean) FROM PUBLIC;
-- aap-statement-break
ALTER FUNCTION aap_inference_routing_v1.routing_step(text, text)
    RENAME TO _routing_step_0279;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_step_0279(text, text)
    FROM PUBLIC;
-- aap-statement-break
DO $revoke_old_login$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'inference_routing_client'
    ) THEN
        EXECUTE 'REVOKE ALL ON FUNCTION '
            || 'aap_inference_routing_v1._routing_step_0279(text, text) '
            || 'FROM inference_routing_client';
    END IF;
END
$revoke_old_login$;
-- aap-statement-break
DO $revoke_private_dispatchers$
DECLARE
    v_function text;
    v_role text;
    v_grantee record;
    v_oid oid;
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'aap_inference_routing_v1._routing_step_0279(text, text)'
    ] LOOP
        v_oid := pg_catalog.to_regprocedure(v_function)::oid;
        FOR v_grantee IN
            SELECT DISTINCT acl.grantee
            FROM pg_catalog.pg_proc AS function
            CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
                function.proacl, pg_catalog.acldefault('f', function.proowner)
            )) AS acl
            WHERE function.oid = v_oid
              AND acl.privilege_type = 'EXECUTE'
              AND acl.grantee NOT IN (0, function.proowner)
        LOOP
            SELECT role.rolname INTO v_role
            FROM pg_catalog.pg_roles AS role
            WHERE role.oid = v_grantee.grantee;
            IF v_role IS NOT NULL THEN
                EXECUTE pg_catalog.format(
                    'REVOKE ALL ON FUNCTION %s FROM %I',v_function,v_role
                );
            END IF;
        END LOOP;
    END LOOP;
END
$revoke_private_dispatchers$;
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
    v_result jsonb;
    v_args jsonb;
    v_scope_id uuid;
    v_operation text;
    v_scope_current boolean;
BEGIN
    BEGIN
        v_result := aap_inference_routing_v1._routing_step_0279(p_action, p_arguments);
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            IF p_action IN ('begin_attempt','heartbeat_scope') THEN
                RETURN aap_inference_routing_v1._routing_rejected('management_busy');
            END IF;
            RAISE;
    END;
    IF v_result ->> 'status' <> 'ok' THEN
        RETURN v_result;
    END IF;
    IF p_action = 'command_status'
       AND v_result #>> '{receipt,operation}' = 'video_acquire'
       AND NOT (v_result #> '{receipt,result}' ? 'reason') THEN
        v_scope_id := aap_inference_routing_v1._routing_uuid(
            v_result #>> '{receipt,result,id}'
        );
        IF NOT EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.scopes AS scope
            WHERE scope.id = v_scope_id AND scope.closed IS FALSE
              AND scope.state IN ('reserved', 'active')
              AND (scope.state <> 'reserved'
                   OR scope.heartbeat_deadline > pg_catalog.clock_timestamp())
              AND ((v_result #> '{receipt,result}')
                   - ARRAY['state','closed','heartbeat_deadline_ms'])
                  = (aap_inference_routing_v1._routing_scope_view(scope.id, true)
                     - ARRAY['state','closed','heartbeat_deadline_ms'])
        ) THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
        END IF;
    ELSIF p_action = 'begin_attempt' THEN
        v_args := p_arguments::jsonb;
        v_scope_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'scope_id');
    ELSE
        RETURN v_result;
    END IF;
    SELECT scope.operation INTO v_operation
    FROM aap_inference_routing_v1.scopes AS scope
    WHERE scope.id = v_scope_id;
    IF v_operation = 'tracker' THEN
        BEGIN
            v_scope_current := aap_jobs_v1._video_execution_scope_is_current(
                v_scope_id, false
            );
        EXCEPTION
            WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_inference_routing_v1._routing_rejected('management_busy');
        END;
        IF NOT v_scope_current THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
        END IF;
    END IF;
    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.routing_step(text, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.video_execution_scope_permits (
    scope_id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    generation bigint NOT NULL CHECK (generation >= 1),
    request_id uuid NOT NULL,
    command_sha256 text NOT NULL CHECK (command_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'),
    transaction_id xid8 NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
REVOKE ALL ON aap_inference_routing_v1.video_execution_scope_permits FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._video_execution_no_scope_rejection(
    p_scope_id uuid,
    p_request_id uuid,
    p_digest text
)
RETURNS boolean
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.command_receipts AS receipt
        WHERE receipt.id = p_request_id AND receipt.command_sha256 = p_digest
          AND receipt.operation = 'video_acquire'
          AND receipt.realm = aap_inference_routing_v1._routing_realm()
          AND aap_inference_routing_v1._routing_exact_keys(receipt.result, ARRAY['reason'])
          AND pg_catalog.jsonb_typeof(receipt.result -> 'reason') = 'string'
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.scopes WHERE id = p_scope_id
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.video_execution_scope_permits
        WHERE scope_id = p_scope_id
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._video_execution_no_scope_rejection(uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
ALTER TABLE aap_inference_routing_v1.command_receipts
    DROP CONSTRAINT command_receipts_operation_check;
-- aap-statement-break
ALTER TABLE aap_inference_routing_v1.command_receipts
    ADD CONSTRAINT command_receipts_operation_check CHECK (
        operation IN ('acquire', 'video_acquire', 'begin', 'settle', 'heartbeat', 'close',
                      'scope_status', 'reconcile', 'prepare_control', 'finish_control',
                      'begin_control', 'settle_control')
    );
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._video_execution_canonical_json(
    p_value jsonb
)
RETURNS text
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
    v_type text := pg_catalog.jsonb_typeof(p_value);
    v_result text;
BEGIN
    IF v_type = 'object' THEN
        SELECT '{' || COALESCE(pg_catalog.string_agg(
                   pg_catalog.to_json(pair.key)::text || ':' ||
                   aap_inference_routing_v1._video_execution_canonical_json(pair.value),
                   ',' ORDER BY pair.key COLLATE "C"
               ), '') || '}'
        INTO v_result
        FROM pg_catalog.jsonb_each(p_value) AS pair(key, value);
        RETURN v_result;
    ELSIF v_type = 'array' THEN
        SELECT '[' || COALESCE(pg_catalog.string_agg(
                   aap_inference_routing_v1._video_execution_canonical_json(item.value),
                   ',' ORDER BY item.ordinality
               ), '') || ']'
        INTO v_result
        FROM pg_catalog.jsonb_array_elements(p_value) WITH ORDINALITY AS item(value, ordinality);
        RETURN v_result;
    END IF;
    RETURN p_value::text;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._video_execution_canonical_json(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._video_execution_scope_uuid(
    p_request_id uuid
)
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
    v_hash bytea;
    v_hex text;
BEGIN
    v_hash := public.digest(
        pg_catalog.uuid_send(p_request_id)
        || pg_catalog.convert_to('aap-video-execution-scope/v1','UTF8'),
        'sha1'
    );
    v_hash := pg_catalog.set_byte(v_hash, 6, (pg_catalog.get_byte(v_hash, 6) & 15) | 80);
    v_hash := pg_catalog.set_byte(v_hash, 8, (pg_catalog.get_byte(v_hash, 8) & 63) | 128);
    v_hex := pg_catalog.encode(pg_catalog.substring(v_hash, 1, 16), 'hex');
    RETURN (
        pg_catalog.substring(v_hex, 1, 8) || '-' ||
        pg_catalog.substring(v_hex, 9, 4) || '-' ||
        pg_catalog.substring(v_hex, 13, 4) || '-' ||
        pg_catalog.substring(v_hex, 17, 4) || '-' ||
        pg_catalog.substring(v_hex, 21, 12)
    )::uuid;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._video_execution_scope_uuid(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._video_execution_result(
    p_data jsonb,
    p_reason text DEFAULT NULL
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
    SELECT CASE WHEN p_reason IS NULL THEN
        pg_catalog.jsonb_build_object(
            'schema','aap-video-execution-data/v1','status','ok'
        ) || COALESCE(p_data,'{}'::jsonb)
    ELSE pg_catalog.jsonb_build_object(
        'schema','aap-video-execution-data/v1','status','rejected','reason',p_reason
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._video_execution_result(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._video_execution_permit_scope()
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
    v_permit aap_inference_routing_v1.video_execution_scope_permits%ROWTYPE;
BEGIN
    IF NEW.operation <> 'tracker' THEN
        RETURN NEW;
    END IF;
    DELETE FROM aap_inference_routing_v1.video_execution_scope_permits AS permit
    WHERE permit.scope_id = NEW.id
      AND permit.transaction_id = pg_catalog.pg_current_xact_id()
      AND permit.command_sha256 = NEW.command_sha256
      AND permit.job_id::text = pg_catalog.substring(NEW.owner, 9)
    RETURNING permit.* INTO v_permit;
    IF NOT FOUND OR NEW.owner IS DISTINCT FROM 'tracker:' || v_permit.job_id::text THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'tracker scope requires execution binding';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._video_execution_permit_scope() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_route_video_execution_scope_permit
BEFORE INSERT ON aap_inference_routing_v1.scopes
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._video_execution_permit_scope();
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._video_execution_attempt_gate()
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
    v_operation text;
BEGIN
    SELECT scope.operation INTO v_operation
    FROM aap_inference_routing_v1.scopes AS scope
    WHERE scope.id = NEW.scope_id;
    IF v_operation = 'tracker'
       AND NOT aap_jobs_v1._video_execution_scope_is_current(NEW.scope_id, false) THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'tracker execution claim is not current';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._video_execution_attempt_gate() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_route_video_execution_attempt_gate
BEFORE INSERT ON aap_inference_routing_v1.attempts
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._video_execution_attempt_gate();
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._video_execution_heartbeat_gate()
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
    IF OLD.operation = 'tracker'
       AND NEW.heartbeat_deadline IS DISTINCT FROM OLD.heartbeat_deadline
       AND NOT aap_jobs_v1._video_execution_scope_is_current(OLD.id, true) THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'tracker execution claim is not current';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._video_execution_heartbeat_gate() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_route_video_execution_heartbeat_gate
BEFORE UPDATE OF heartbeat_deadline ON aap_inference_routing_v1.scopes
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._video_execution_heartbeat_gate();
-- aap-statement-break
ALTER FUNCTION aap_inference_routing_v1.scope_retirement_step(text, text)
    RENAME TO _scope_retirement_step_0280;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._scope_retirement_step_0280(text, text) FROM PUBLIC;
-- aap-statement-break
DO $revoke_old_retirement_login$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'inference_routing_client'
    ) THEN
        EXECUTE 'REVOKE ALL ON FUNCTION '
            || 'aap_inference_routing_v1._scope_retirement_step_0280(text, text) '
            || 'FROM inference_routing_client';
    END IF;
END
$revoke_old_retirement_login$;
-- aap-statement-break
DO $revoke_private_retirement_dispatcher$
DECLARE
    v_grantee record;
    v_role text;
    v_oid oid := pg_catalog.to_regprocedure(
        'aap_inference_routing_v1._scope_retirement_step_0280(text,text)'
    )::oid;
BEGIN
    FOR v_grantee IN
        SELECT DISTINCT acl.grantee
        FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
            function.proacl, pg_catalog.acldefault('f', function.proowner)
        )) AS acl
        WHERE function.oid = v_oid
          AND acl.privilege_type = 'EXECUTE'
          AND acl.grantee NOT IN (0, function.proowner)
    LOOP
        SELECT role.rolname INTO v_role
        FROM pg_catalog.pg_roles AS role
        WHERE role.oid = v_grantee.grantee;
        IF v_role IS NOT NULL THEN
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON FUNCTION '
                || 'aap_inference_routing_v1._scope_retirement_step_0280(text, text) '
                || 'FROM %I',v_role
            );
        END IF;
    END LOOP;
END
$revoke_private_retirement_dispatcher$;
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
    v_result jsonb;
BEGIN
    v_result := aap_inference_routing_v1._scope_retirement_step_0280(p_action,p_arguments);
    IF p_action = 'policy' AND v_result ->> 'status' = 'ok' THEN
        v_result := pg_catalog.jsonb_set(
            v_result,
            '{functions}',
            (v_result -> 'functions') || pg_catalog.jsonb_build_array(
                'aap_inference_routing_v1.video_execution_scope_step(text,text)'
            ),
            false
        );
    END IF;
    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.scope_retirement_step(text, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1.video_execution_scope_step(
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
SET lock_timeout = '250ms'
AS $function$
DECLARE
    v_args jsonb;
    v_jobs_args jsonb;
    v_jobs_reply jsonb;
    v_command jsonb;
    v_body jsonb;
    v_snapshot jsonb;
    v_intent jsonb;
    v_scope jsonb;
    v_result jsonb;
    v_receipt record;
    v_keys text[];
    v_job_id uuid;
    v_request_id uuid;
    v_scope_id uuid;
    v_run_id uuid;
    v_worker_id uuid;
    v_generation bigint;
    v_token uuid;
    v_sha text;
    v_prior_scope uuid;
    v_retired boolean := false;
    v_ttl bigint;
    v_reason text;
    v_jobs_policy jsonb;
BEGIN
    IF p_arguments IS NULL OR pg_catalog.octet_length(p_arguments) > 2097152 THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL, 'invalid_command');
    END IF;
    BEGIN
        v_args := p_arguments::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL, 'invalid_command');
    END;
    IF pg_catalog.jsonb_typeof(v_args) IS DISTINCT FROM 'object' THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL, 'invalid_command');
    END IF;
    IF p_action = 'policy' THEN
        IF NOT aap_inference_routing_v1._routing_exact_keys(v_args, ARRAY[]::text[]) THEN
            RETURN aap_inference_routing_v1._video_execution_result(NULL, 'invalid_command');
        END IF;
        v_jobs_policy := aap_jobs_v1._video_execution_route_step('policy','{}');
        RETURN aap_inference_routing_v1._video_execution_result(pg_catalog.jsonb_build_object(
            'ready', aap_inference_routing_v1._routing_secret() IS NOT NULL
                AND v_jobs_policy ->> 'ready' = 'true',
            'functions', pg_catalog.jsonb_build_array(
                'aap_inference_routing_v1.routing_policy()',
                'aap_inference_routing_v1.routing_step(text,text)',
                'aap_inference_routing_v1.scope_retirement_step(text,text)',
                'aap_inference_routing_v1.video_execution_scope_step(text,text)'
            )
        ));
    END IF;
    IF p_action = 'prepare' THEN
        v_keys := ARRAY[
            'job_id','run_id','worker_id','generation','lease_token','request_id',
            'command','command_sha256','scope_id'
        ];
    ELSIF p_action = 'reserve' THEN
        v_keys := ARRAY[
            'job_id','run_id','worker_id','generation','lease_token','request_id',
            'command_sha256','snapshot','candidate_ids','scope_id','ttl_ms'
        ];
    ELSIF p_action = 'status' THEN
        v_keys := ARRAY['job_id','run_id','worker_id','generation','lease_token'];
    ELSIF p_action = 'record' THEN
        v_keys := ARRAY[
            'job_id','run_id','worker_id','generation','lease_token',
            'request_id','command_sha256','result'
        ];
    ELSE
        RETURN aap_inference_routing_v1._video_execution_result(NULL, 'invalid_command');
    END IF;
    IF NOT aap_inference_routing_v1._routing_exact_keys(v_args, v_keys) THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL, 'invalid_command');
    END IF;
    BEGIN
        v_job_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'job_id');
        v_run_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'run_id');
        v_worker_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'worker_id');
        v_token := aap_inference_routing_v1._routing_uuid(v_args ->> 'lease_token');
        v_generation := aap_inference_routing_v1._routing_integer(
            v_args -> 'generation', 1, 9223372036854775807
        );
        IF p_action <> 'status' THEN
            v_request_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'request_id');
            v_sha := v_args ->> 'command_sha256';
            IF pg_catalog.jsonb_typeof(v_args -> 'command_sha256') IS DISTINCT FROM 'string'
               OR v_sha !~ '^[0-9a-f]{64}$' THEN
                RETURN aap_inference_routing_v1._video_execution_result(NULL, 'invalid_command');
            END IF;
        END IF;
        IF p_action IN ('prepare','reserve') THEN
            v_scope_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'scope_id');
        END IF;
    EXCEPTION WHEN SQLSTATE '22023' THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL, 'invalid_command');
    END;
    IF p_action = 'prepare'
       AND v_scope_id IS DISTINCT FROM
           aap_inference_routing_v1._video_execution_scope_uuid(v_request_id) THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'invalid_command');
    END IF;

    v_jobs_args := pg_catalog.jsonb_build_object(
        'job_id',v_job_id,'run_id',v_run_id,'worker_id',v_worker_id,
        'generation',v_generation,'lease_token',v_token
    );
    IF p_action = 'status' THEN
        v_jobs_reply := aap_jobs_v1._video_execution_route_step('status', v_jobs_args::text);
        IF v_jobs_reply ->> 'ok' IS DISTINCT FROM 'true' THEN
            RETURN aap_inference_routing_v1._video_execution_result(
                NULL, COALESCE(v_jobs_reply ->> 'reason','scope_unavailable')
            );
        END IF;
        v_intent := v_jobs_reply -> 'intent';
        IF v_intent IS NOT NULL AND v_intent <> 'null'::jsonb
           AND v_intent ->> 'state' = 'bound' THEN
            v_scope_id := aap_inference_routing_v1._routing_uuid(v_intent ->> 'scope_id');
            v_scope := aap_inference_routing_v1._routing_scope_view(v_scope_id, true);
            IF v_scope IS NULL OR NOT EXISTS (
                SELECT 1 FROM aap_inference_routing_v1.scopes AS stored_scope
                WHERE stored_scope.id = v_scope_id
                  AND stored_scope.command_sha256 = v_intent ->> 'command_sha256'
                  AND stored_scope.owner = 'tracker:' || v_job_id::text
                  AND stored_scope.operation = 'tracker'
            ) THEN
                RETURN aap_inference_routing_v1._video_execution_result(NULL, 'scope_unavailable');
            END IF;
            IF v_scope ->> 'id' IS DISTINCT FROM v_intent ->> 'scope_id' THEN
                RETURN aap_inference_routing_v1._video_execution_result(NULL, 'scope_unavailable');
            END IF;
        ELSE
            v_scope := NULL;
        END IF;
        RETURN aap_inference_routing_v1._video_execution_result(
            pg_catalog.jsonb_build_object('intent', v_intent, 'scope', v_scope)
        );
    END IF;

    IF p_action = 'prepare' THEN
        IF pg_catalog.jsonb_typeof(v_args -> 'command_sha256') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_args -> 'command') IS DISTINCT FROM 'object' THEN
            RETURN aap_inference_routing_v1._video_execution_result(NULL, 'invalid_command');
        END IF;
        -- Read the previous immutable intent before installing a new one. A
        -- bound old route is superseded only after its exact 0280 retirement
        -- has a completed+reconciled fact in this current database realm.
        v_jobs_reply := aap_jobs_v1._video_execution_route_step('status', v_jobs_args::text);
        IF v_jobs_reply ->> 'ok' IS DISTINCT FROM 'true' THEN
            RETURN aap_inference_routing_v1._video_execution_result(
                NULL, COALESCE(v_jobs_reply ->> 'reason','scope_unavailable')
            );
        END IF;
        v_intent := v_jobs_reply -> 'intent';
        IF v_intent IS NOT NULL AND v_intent <> 'null'::jsonb
           AND v_intent ->> 'state' = 'bound'
           AND v_intent ->> 'request_id' IS DISTINCT FROM v_request_id::text THEN
            v_prior_scope := aap_inference_routing_v1._routing_uuid(v_intent ->> 'scope_id');
            SELECT EXISTS (
                SELECT 1
                FROM aap_inference_routing_v1.scope_retirements AS retirement
                JOIN aap_inference_routing_v1.scopes AS scope
                  ON scope.id = retirement.scope_id
                WHERE retirement.scope_id = v_prior_scope
                  AND retirement.phase = 'completed'
                  AND retirement.realm = aap_inference_routing_v1._routing_realm()
                  AND scope.state = 'reconciled'
            ) INTO v_retired;
            IF NOT v_retired THEN
                RETURN aap_inference_routing_v1._video_execution_result(NULL, 'recovery_required');
            END IF;
        END IF;
        v_jobs_args := v_jobs_args || pg_catalog.jsonb_build_object(
            'request_id',v_request_id,'command',v_args -> 'command',
            'command_sha256',v_sha,'scope_id',v_scope_id,
            'retired_prior_scope_id',CASE WHEN v_retired THEN v_prior_scope::text ELSE NULL END
        );
        v_jobs_reply := aap_jobs_v1._video_execution_route_step('prepare',v_jobs_args::text);
        IF v_jobs_reply ->> 'ok' IS DISTINCT FROM 'true' THEN
            RETURN aap_inference_routing_v1._video_execution_result(
                NULL, COALESCE(v_jobs_reply ->> 'reason','scope_unavailable')
            );
        END IF;
        v_intent := v_jobs_reply -> 'intent';
        IF v_intent ->> 'state' = 'bound' THEN
            v_scope := aap_inference_routing_v1._routing_scope_view(v_scope_id, true);
            IF v_scope IS NULL OR NOT EXISTS (
                SELECT 1 FROM aap_inference_routing_v1.scopes AS stored_scope
                WHERE stored_scope.id = v_scope_id
                  AND stored_scope.command_sha256 = v_sha
                  AND stored_scope.owner = 'tracker:' || v_job_id::text
                  AND stored_scope.operation = 'tracker'
            ) THEN
                RETURN aap_inference_routing_v1._video_execution_result(NULL, 'scope_unavailable');
            END IF;
        ELSE
            v_scope := NULL;
        END IF;
        RETURN aap_inference_routing_v1._video_execution_result(
            pg_catalog.jsonb_build_object('intent',v_intent,'scope',v_scope)
        );
    END IF;

    IF p_action = 'reserve' THEN
        IF pg_catalog.jsonb_typeof(v_args -> 'snapshot') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_args -> 'candidate_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(v_args -> 'ttl_ms') IS DISTINCT FROM 'number'
           OR (v_args ->> 'ttl_ms') !~ '^[1-9][0-9]*$' THEN
            RETURN aap_inference_routing_v1._video_execution_result(NULL,'invalid_command');
        END IF;
        v_ttl := (v_args ->> 'ttl_ms')::bigint;
        IF v_ttl > 2147483647 THEN
            RETURN aap_inference_routing_v1._video_execution_result(NULL,'invalid_command');
        END IF;
        v_jobs_args := v_jobs_args || pg_catalog.jsonb_build_object(
            'request_id',v_request_id,'command_sha256',v_sha,'scope_id',v_scope_id
        );
        v_jobs_reply := aap_jobs_v1._video_execution_route_step('reserve_check',v_jobs_args::text);
        IF v_jobs_reply ->> 'ok' IS DISTINCT FROM 'true' THEN
            RETURN aap_inference_routing_v1._video_execution_result(
                NULL, COALESCE(v_jobs_reply ->> 'reason','scope_unavailable')
            );
        END IF;
        v_intent := v_jobs_reply -> 'intent';
        v_command := v_jobs_reply -> 'command';
        v_body := v_command -> 'body';
        v_snapshot := aap_inference_routing_v1._routing_open_snapshot(
            v_args ->> 'snapshot'
        );
        IF pg_catalog.jsonb_typeof(v_body) IS DISTINCT FROM 'object'
           OR v_snapshot IS NULL
           OR NOT aap_inference_routing_v1._routing_exact_keys(
                v_snapshot, ARRAY[
                    'schema','realm','xid','mode','pool_id','physical_pool_id',
                    'physical_generation','registry_id','project_id','required_instance_id',
                    'generation','health_max_age_seconds','pool_enabled',
                    'legacy_instance_id','members'
                ]
           )
           OR v_snapshot ->> 'schema' IS DISTINCT FROM 'aap-inference-routing-snapshot/v1'
           OR v_snapshot ->> 'realm' IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
           OR v_snapshot ->> 'xid' IS DISTINCT FROM pg_catalog.pg_current_xact_id()::text
           OR v_body ->> 'mode' IS DISTINCT FROM v_snapshot ->> 'mode'
           OR v_body -> 'pool_id' IS DISTINCT FROM v_snapshot -> 'pool_id'
           OR v_body -> 'registry_id' IS DISTINCT FROM v_snapshot -> 'registry_id'
           OR v_body -> 'project_id' IS DISTINCT FROM v_snapshot -> 'project_id'
           OR v_body -> 'required_instance_id' IS DISTINCT FROM
                v_snapshot -> 'required_instance_id' THEN
            RETURN aap_inference_routing_v1._video_execution_result(NULL,'invalid_command');
        END IF;
        IF v_intent ->> 'state' = 'bound' THEN
            v_scope := aap_inference_routing_v1._routing_scope_view(v_scope_id, true);
            IF v_scope IS NULL OR NOT EXISTS (
                SELECT 1 FROM aap_inference_routing_v1.scopes AS stored_scope
                WHERE stored_scope.id = v_scope_id
                  AND stored_scope.command_sha256 = v_sha
                  AND stored_scope.owner = 'tracker:' || v_job_id::text
                  AND stored_scope.operation = 'tracker'
            ) THEN
                RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_unavailable');
            END IF;
            RETURN aap_inference_routing_v1._video_execution_result(
                pg_catalog.jsonb_build_object('intent',v_intent,'scope',v_scope)
            );
        END IF;
        IF v_intent ->> 'state' IS DISTINCT FROM 'prepared' THEN
            RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_mismatch');
        END IF;
        INSERT INTO aap_inference_routing_v1.video_execution_scope_permits (
            scope_id,job_id,run_id,generation,request_id,command_sha256,transaction_id
        ) VALUES (
            v_scope_id,v_job_id,v_run_id,v_generation,v_request_id,v_sha,
            pg_catalog.pg_current_xact_id()
        );
        v_result := aap_inference_routing_v1.routing_step('reserve_scope',
            pg_catalog.jsonb_build_object(
                'snapshot',v_args -> 'snapshot',
                'candidate_ids',v_args -> 'candidate_ids',
                'scope_id',v_scope_id,
                'command_sha256',v_sha,
                'owner','tracker:' || v_job_id::text,
                'operation','tracker',
                'ttl_ms',v_ttl
            )::text
        );
        IF v_result ->> 'status' = 'rejected' THEN
            DELETE FROM aap_inference_routing_v1.video_execution_scope_permits
            WHERE scope_id = v_scope_id AND transaction_id = pg_catalog.pg_current_xact_id();
            RETURN aap_inference_routing_v1._video_execution_result(
                NULL, COALESCE(v_result ->> 'reason','scope_unavailable')
            );
        END IF;
        IF v_result ->> 'status' IS DISTINCT FROM 'ok'
           OR NOT aap_inference_routing_v1._routing_exact_keys(
                v_result, ARRAY['schema','status','scope']
           ) OR v_result ->> 'schema' IS DISTINCT FROM 'aap-inference-routing-data/v1'
           OR pg_catalog.jsonb_typeof(v_result -> 'scope') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'invalid Native tracker reservation';
        END IF;
        v_scope := v_result -> 'scope';
        IF v_scope ->> 'id' IS DISTINCT FROM v_scope_id::text
           OR v_scope ->> 'owner' IS DISTINCT FROM 'tracker:' || v_job_id::text
           OR v_scope ->> 'operation' IS DISTINCT FROM 'tracker' THEN
            RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'tracker reservation identity mismatch';
        END IF;
        v_jobs_reply := aap_jobs_v1._video_execution_route_step('bind',v_jobs_args::text);
        IF v_jobs_reply ->> 'ok' IS DISTINCT FROM 'true'
           OR v_jobs_reply #>> '{intent,state}' IS DISTINCT FROM 'bound' THEN
            RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'tracker route binding lost';
        END IF;
        DELETE FROM aap_inference_routing_v1.video_execution_scope_permits
        WHERE scope_id = v_scope_id AND transaction_id = pg_catalog.pg_current_xact_id();
        RETURN aap_inference_routing_v1._video_execution_result(pg_catalog.jsonb_build_object(
            'intent',v_jobs_reply -> 'intent','scope',v_scope
        ));
    END IF;

    -- record is inside the same caller-owned transaction as scope reservation
    -- or a closed policy rejection. This keeps video_acquire out of the frozen
    -- 0279 generic receipt action while preserving command_status replay.
    v_jobs_args := v_jobs_args || pg_catalog.jsonb_build_object(
        'request_id',v_request_id,'command_sha256',v_sha,'scope_id',
        COALESCE(v_args -> 'scope_id','null'::jsonb)
    );
    -- record requests carry no scope_id; remove the synthetic field before the
    -- narrow Jobs identity check.
    v_jobs_args := v_jobs_args - 'scope_id';
    v_jobs_reply := aap_jobs_v1._video_execution_route_step('record_check',v_jobs_args::text);
    IF v_jobs_reply ->> 'ok' IS DISTINCT FROM 'true' THEN
        RETURN aap_inference_routing_v1._video_execution_result(
            NULL, COALESCE(v_jobs_reply ->> 'reason','scope_unavailable')
        );
    END IF;
    v_intent := v_jobs_reply -> 'intent';
    v_result := v_args -> 'result';
    IF pg_catalog.jsonb_typeof(v_result) IS DISTINCT FROM 'object'
       OR pg_catalog.octet_length(v_result::text) > 65536
       OR NOT aap_inference_routing_v1._routing_receipt_safe(v_result) THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'invalid_command');
    END IF;
    IF aap_inference_routing_v1._routing_exact_keys(v_result, ARRAY['reason']) THEN
        v_reason := v_result ->> 'reason';
        IF pg_catalog.jsonb_typeof(v_result -> 'reason') IS DISTINCT FROM 'string'
           OR v_reason NOT IN (
               'invalid_command','pool_unavailable','pool_not_enabled','pool_saturated',
               'generation_mismatch','backend_not_ready','continuity_unverified',
               'management_busy','scope_unavailable','scope_expired','scope_closed',
               'transport_changed','scope_mismatch'
           ) THEN
            RETURN aap_inference_routing_v1._video_execution_result(NULL,'invalid_command');
        END IF;
    ELSIF v_intent ->> 'state' = 'bound' THEN
        v_scope_id := aap_inference_routing_v1._routing_uuid(v_intent ->> 'scope_id');
        v_scope := aap_inference_routing_v1._routing_scope_view(v_scope_id,true);
        IF v_scope IS NULL OR v_result IS DISTINCT FROM v_scope THEN
            RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_mismatch');
        END IF;
    ELSE
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_mismatch');
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-routing-command:' || v_request_id::text, 412091)
    );
    SELECT receipt.id,receipt.command_sha256,receipt.operation,receipt.result,
           receipt.creating_xid,receipt.realm
    INTO v_receipt
    FROM aap_inference_routing_v1.command_receipts AS receipt
    WHERE receipt.id = v_request_id FOR UPDATE;
    IF FOUND THEN
        IF v_receipt.command_sha256 IS DISTINCT FROM v_sha
           OR v_receipt.operation IS DISTINCT FROM 'video_acquire'
           OR v_receipt.result IS DISTINCT FROM v_result
           OR v_receipt.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm() THEN
            RETURN aap_inference_routing_v1._video_execution_result(NULL,'intent_conflict');
        END IF;
    ELSE
        INSERT INTO aap_inference_routing_v1.command_receipts (
            id,command_sha256,operation,result,creating_xid,realm
        ) VALUES (
            v_request_id,v_sha,'video_acquire',v_result,
            pg_catalog.pg_current_xact_id(),aap_inference_routing_v1._routing_realm()
        );
        SELECT v_request_id AS id,v_sha AS command_sha256,'video_acquire'::text AS operation,
               v_result AS result,pg_catalog.pg_current_xact_id() AS creating_xid,
               aap_inference_routing_v1._routing_realm() AS realm
        INTO v_receipt;
    END IF;
    RETURN aap_inference_routing_v1._video_execution_result(pg_catalog.jsonb_build_object(
        'receipt',pg_catalog.jsonb_build_object(
            'request_id',v_request_id,
            'command_sha256',v_receipt.command_sha256,
            'operation',v_receipt.operation,
            'state',CASE WHEN v_receipt.creating_xid = pg_catalog.pg_current_xact_id()
                         THEN 'caller_pending' ELSE 'committed' END,
            'result',v_receipt.result
        )
    ));
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'execution_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.video_execution_scope_step(text, text)
    FROM PUBLIC;
-- aap-statement-break
