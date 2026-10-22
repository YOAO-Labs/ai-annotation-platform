-- Coordinated restore hold and proof for copied video execution authority.
-- All state transitions remain private to the owning Native modules.
CREATE TABLE aap_inference_routing_v1.video_restore_function_restore (
    signature text PRIMARY KEY,
    function_definition text NOT NULL
);
-- aap-statement-break
INSERT INTO aap_inference_routing_v1.video_restore_function_restore(
    signature,function_definition
)
SELECT function.oid::pg_catalog.regprocedure::text,
       pg_catalog.pg_get_functiondef(function.oid)
FROM pg_catalog.pg_proc AS function
WHERE function.oid IN (
    pg_catalog.to_regprocedure('aap_jobs_v1._video_execution_secret()')::oid,
    pg_catalog.to_regprocedure('aap_inference_routing_v1._routing_secret()')::oid,
    pg_catalog.to_regprocedure(
        'aap_inference_routing_v1.video_execution_scope_retired(uuid)'
    )::oid,
    pg_catalog.to_regprocedure(
        'aap_inference_routing_v1._video_execution_no_scope_rejection(uuid,uuid,text)'
    )::oid,
    pg_catalog.to_regprocedure(
        'aap_inference_routing_v1.video_execution_scope_step(text,text)'
    )::oid
);
-- aap-statement-break
REVOKE ALL ON aap_inference_routing_v1.video_restore_function_restore FROM PUBLIC;
-- aap-statement-break
DO $restore_function_guard$
BEGIN
    IF (SELECT pg_catalog.count(*)
        FROM aap_inference_routing_v1.video_restore_function_restore) <> 5 THEN
        RAISE EXCEPTION USING
            ERRCODE='55000',
            MESSAGE='required Native video functions for restore downgrade are unavailable';
    END IF;
END
$restore_function_guard$;
-- aap-statement-break
CREATE TABLE aap_jobs_v1.video_restore_fence (
    singleton boolean PRIMARY KEY CHECK (singleton IS TRUE),
    restore_id uuid NOT NULL UNIQUE,
    source_jobs_realm text NOT NULL,
    target_jobs_realm text NOT NULL,
    source_jobs_secret_sha256 text NOT NULL CHECK (
        source_jobs_secret_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    route_snapshot jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(route_snapshot) = 'array'
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.video_restore_fence FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.video_restore_records (
    restore_id uuid PRIMARY KEY,
    command jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(command) = 'object'),
    command_sha256 text NOT NULL CHECK (command_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'),
    backup_sha256 text NOT NULL CHECK (backup_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'),
    fence_sha256 text NOT NULL CHECK (fence_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'),
    source_jobs_realm text NOT NULL,
    target_jobs_realm text NOT NULL,
    source_routing_realm text NOT NULL,
    target_routing_realm text NOT NULL,
    source_jobs_secret_sha256 text NOT NULL CHECK (
        source_jobs_secret_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    source_routing_secret_sha256 text NOT NULL CHECK (
        source_routing_secret_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    registry_state_snapshot jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(registry_state_snapshot) = 'array'
    ),
    phase text NOT NULL CHECK (phase IN ('prepared','completed')),
    started_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    completed_at timestamptz,
    counts jsonb CHECK (
        counts IS NULL OR (
            pg_catalog.jsonb_typeof(counts) = 'object'
            AND (counts - ARRAY[
                'executions_invalidated','route_intents_rewrapped',
                'registries_rebound','scopes_retired','attempts_reconciled'
            ]) = '{}'::jsonb
            AND counts ?& ARRAY[
                'executions_invalidated','route_intents_rewrapped',
                'registries_rebound','scopes_retired','attempts_reconciled'
            ]
            AND (counts ->> 'executions_invalidated') ~ '^(0|[1-9][0-9]*)$'
            AND (counts ->> 'route_intents_rewrapped') ~ '^(0|[1-9][0-9]*)$'
            AND (counts ->> 'registries_rebound') ~ '^(0|[1-9][0-9]*)$'
            AND (counts ->> 'scopes_retired') ~ '^(0|[1-9][0-9]*)$'
            AND (counts ->> 'attempts_reconciled') ~ '^(0|[1-9][0-9]*)$'
        )
    ),
    CHECK ((phase = 'prepared' AND completed_at IS NULL AND counts IS NULL)
        OR (phase = 'completed' AND completed_at IS NOT NULL AND counts IS NOT NULL))
);
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.video_restore_contexts (
    restore_id uuid NOT NULL REFERENCES aap_inference_routing_v1.video_restore_records(restore_id)
        ON DELETE RESTRICT,
    control_id uuid NOT NULL UNIQUE,
    registry_id uuid NOT NULL,
    phase text NOT NULL CHECK (phase IN ('prepared','bound')),
    new_epoch uuid NOT NULL,
    challenge text NOT NULL CHECK (challenge COLLATE "C" ~ '^[0-9a-f]{64}$'),
    retired_epoch uuid,
    backend_incarnation uuid NOT NULL,
    endpoint jsonb NOT NULL CHECK (
        endpoint='{}'::jsonb OR (
            pg_catalog.jsonb_typeof(endpoint) = 'object'
            AND (endpoint - ARRAY['url','auth_method','auth_token']) = '{}'::jsonb
            AND endpoint ?& ARRAY['url','auth_method','auth_token']
        )
    ),
    signing_key text NOT NULL,
    transport_identity_sha256 text NOT NULL CHECK (
        transport_identity_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    registry_snapshot jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(registry_snapshot) = 'object'
    ),
    transport_snapshot jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(transport_snapshot) = 'object'
    ),
    observed_reply jsonb,
    bound_reply jsonb,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (restore_id,registry_id),
    UNIQUE (restore_id,control_id),
    CHECK ((phase = 'prepared' AND bound_reply IS NULL)
        OR (phase = 'bound' AND bound_reply IS NOT NULL))
);
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.video_restore_scopes (
    restore_id uuid NOT NULL REFERENCES aap_inference_routing_v1.video_restore_records(restore_id)
        ON DELETE RESTRICT,
    scope_id uuid NOT NULL,
    registry_id uuid NOT NULL,
    scope_realm text NOT NULL,
    original_epoch uuid NOT NULL,
    scope_snapshot jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(scope_snapshot) = 'object'
    ),
    PRIMARY KEY (restore_id,scope_id)
);
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.video_restore_attempts (
    restore_id uuid NOT NULL,
    scope_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    attempt_snapshot jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(attempt_snapshot) = 'object'
    ),
    PRIMARY KEY (restore_id,attempt_id),
    FOREIGN KEY (restore_id,scope_id)
        REFERENCES aap_inference_routing_v1.video_restore_scopes(restore_id,scope_id)
        ON DELETE RESTRICT
);
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.video_restore_scope_retirements (
    scope_id uuid NOT NULL,
    restore_id uuid NOT NULL REFERENCES aap_inference_routing_v1.video_restore_records(restore_id)
        ON DELETE RESTRICT,
    registry_id uuid NOT NULL,
    source_routing_realm text NOT NULL,
    target_routing_realm text NOT NULL,
    scope_realm text NOT NULL,
    original_epoch uuid NOT NULL,
    retired_epoch uuid NOT NULL,
    new_epoch uuid NOT NULL,
    backend_incarnation uuid NOT NULL,
    control_id uuid NOT NULL,
    challenge text NOT NULL CHECK (challenge COLLATE "C" ~ '^[0-9a-f]{64}$'),
    transport_identity_sha256 text NOT NULL CHECK (
        transport_identity_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (restore_id,scope_id),
    FOREIGN KEY (restore_id,registry_id)
        REFERENCES aap_inference_routing_v1.video_restore_contexts(restore_id,registry_id)
        ON DELETE RESTRICT
);
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.video_restore_no_scope_receipts (
    restore_id uuid NOT NULL REFERENCES aap_inference_routing_v1.video_restore_records(restore_id)
        ON DELETE RESTRICT,
    request_id uuid NOT NULL,
    scope_id uuid NOT NULL,
    command_sha256 text NOT NULL CHECK (command_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'),
    receipt_realm text NOT NULL,
    source_routing_realm text NOT NULL,
    target_routing_realm text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (restore_id,request_id),
    UNIQUE (restore_id,scope_id)
);
-- aap-statement-break
REVOKE ALL ON ALL TABLES IN SCHEMA aap_inference_routing_v1 FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1._video_restore_reply(
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
            'schema','aap-video-restore-data/v1','status','ok'
        ) || COALESCE(p_data,'{}'::jsonb)
    ELSE pg_catalog.jsonb_build_object(
        'schema','aap-video-restore-data/v1','status','rejected','reason',p_reason
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._video_restore_reply(jsonb,text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1._video_restore_exact_keys(
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
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._video_restore_exact_keys(jsonb,text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1._video_restore_uuid(p_value jsonb)
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
        RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid restore UUID';
    END IF;
    v_text := p_value #>> '{}';
    BEGIN
        v_uuid := v_text::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid restore UUID';
    END;
    IF v_uuid::text IS DISTINCT FROM v_text THEN
        RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid restore UUID';
    END IF;
    RETURN v_uuid;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._video_restore_uuid(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1._video_restore_digest_valid(p_value text)
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
    SELECT p_value COLLATE "C" ~ '^[0-9a-f]{64}$'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._video_restore_digest_valid(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1._video_restore_command_sha256(p_value jsonb)
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
    SELECT pg_catalog.encode(public.digest(
        pg_catalog.convert_to(
            aap_inference_routing_v1._video_execution_canonical_json(p_value),
            'UTF8'
        ),'sha256'
    ),'hex')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._video_restore_command_sha256(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1._video_restore_record(p_restore_id uuid)
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
        'restore_id',record.restore_id,
        'phase',record.phase,
        'command_sha256',record.command_sha256,
        'backup_sha256',record.backup_sha256,
        'fence_sha256',record.fence_sha256,
        'source_jobs_realm',record.source_jobs_realm,
        'target_jobs_realm',record.target_jobs_realm,
        'source_routing_realm',record.source_routing_realm,
        'target_routing_realm',record.target_routing_realm,
        'started_at',pg_catalog.to_char(
            record.started_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
        ),
        'completed_at',CASE WHEN record.completed_at IS NULL THEN NULL ELSE
            pg_catalog.to_char(
                record.completed_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
            ) END,
        'counts',record.counts
    )
    FROM aap_inference_routing_v1.video_restore_records AS record
    WHERE record.restore_id=p_restore_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._video_restore_record(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1._video_restore_contexts(p_restore_id uuid)
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
    SELECT CASE WHEN record.phase='completed' THEN '[]'::jsonb ELSE
        COALESCE((
            SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                'control_id',context.control_id,
                'registry_id',context.registry_id,
                'phase',context.phase,
                'new_epoch',context.new_epoch,
                'retired_epoch',context.retired_epoch,
                'challenge',context.challenge,
                'backend_incarnation',context.backend_incarnation,
                'endpoint',context.endpoint,
                'signing_key',context.signing_key
            ) ORDER BY context.registry_id)
            FROM aap_inference_routing_v1.video_restore_contexts AS context
            WHERE context.restore_id=record.restore_id
        ),'[]'::jsonb)
    END
    FROM aap_inference_routing_v1.video_restore_records AS record
    WHERE record.restore_id=p_restore_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._video_restore_contexts(uuid)
    FROM PUBLIC;
-- All ordinary video/routing authorities take this transaction-level shared
-- barrier before re-reading their secret. Restore prepare/finish take it
-- exclusively. The database name is unique within the connected cluster.
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_execution_secret()
RETURNS bytea
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
    v_secret bytea;
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RETURN NULL;
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock_shared(
        pg_catalog.hashtextextended(
            'aap-native-authority-fence/v1:' || pg_catalog.current_database(),284
        )
    );
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.video_restore_fence) THEN
        RETURN NULL;
    END IF;
    SELECT authority.secret INTO v_secret
    FROM aap_jobs_v1.video_execution_authority AS authority
    JOIN pg_catalog.pg_database AS database
      ON database.oid=authority.database_oid
     AND database.datname=authority.database_name
    JOIN LATERAL pg_catalog.pg_control_system() AS control
      ON control.system_identifier=authority.cluster_identifier
    WHERE authority.singleton IS TRUE
      AND database.datname=pg_catalog.current_database();
    RETURN v_secret;
END
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_secret()
RETURNS bytea
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
    v_secret bytea;
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RETURN NULL;
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock_shared(
        pg_catalog.hashtextextended(
            'aap-native-authority-fence/v1:' || pg_catalog.current_database(),284
        )
    );
    IF EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.video_restore_records
        WHERE phase='prepared'
    ) THEN
        RETURN NULL;
    END IF;
    SELECT secret.secret INTO v_secret
    FROM aap_inference_routing_v1.authority_secret AS secret
    JOIN pg_catalog.pg_database AS database
      ON database.oid=secret.realm_database_oid
     AND database.datname=secret.realm_database_name
    WHERE secret.singleton IS TRUE
      AND database.datname=pg_catalog.current_database();
    RETURN v_secret;
END
$function$;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1._video_restore_record_guard()
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
    IF TG_OP='DELETE' OR OLD.phase IS DISTINCT FROM 'prepared'
       OR NEW.phase IS DISTINCT FROM 'completed'
       OR NEW.completed_at IS NULL OR NEW.counts IS NULL
       OR (pg_catalog.to_jsonb(NEW)
           - ARRAY['phase','completed_at','counts'])
          IS DISTINCT FROM
          (pg_catalog.to_jsonb(OLD)
           - ARRAY['phase','completed_at','counts']) THEN
        RAISE EXCEPTION USING ERRCODE='55000',
            MESSAGE='video restore record is immutable';
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._video_restore_record_guard() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_video_restore_record_guard
BEFORE UPDATE OR DELETE ON aap_inference_routing_v1.video_restore_records
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._video_restore_record_guard();
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1._video_restore_context_guard()
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
    v_record_phase text;
    v_old jsonb;
    v_new jsonb;
BEGIN
    SELECT record.phase INTO v_record_phase
    FROM aap_inference_routing_v1.video_restore_records AS record
    WHERE record.restore_id=COALESCE(NEW.restore_id,OLD.restore_id);
    IF TG_OP='INSERT' THEN
        IF v_record_phase IS DISTINCT FROM 'prepared' THEN
            RAISE EXCEPTION USING ERRCODE='55000',
                MESSAGE='video restore context is immutable';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP='DELETE' OR v_record_phase IS DISTINCT FROM 'prepared' THEN
        RAISE EXCEPTION USING ERRCODE='55000',
            MESSAGE='video restore context is immutable';
    END IF;
    v_old := pg_catalog.to_jsonb(OLD);
    v_new := pg_catalog.to_jsonb(NEW);
    IF OLD.phase='prepared' AND NEW.phase='prepared'
       AND OLD.retired_epoch IS NULL AND NEW.retired_epoch IS NOT NULL
       AND OLD.observed_reply IS NULL AND NEW.observed_reply IS NOT NULL
       AND NEW.bound_reply IS NULL
       AND (v_new - ARRAY['retired_epoch','observed_reply'])
           = (v_old - ARRAY['retired_epoch','observed_reply']) THEN
        RETURN NEW;
    END IF;
    IF OLD.phase='prepared' AND NEW.phase='bound'
       AND OLD.retired_epoch IS NOT NULL
       AND NEW.retired_epoch IS NOT DISTINCT FROM OLD.retired_epoch
       AND NEW.observed_reply IS NOT DISTINCT FROM OLD.observed_reply
       AND OLD.bound_reply IS NULL AND NEW.bound_reply IS NOT NULL
       AND (v_new - ARRAY['phase','bound_reply'])
           = (v_old - ARRAY['phase','bound_reply']) THEN
        RETURN NEW;
    END IF;
    IF OLD.phase='bound' AND NEW.phase='bound'
       AND OLD.bound_reply IS NOT NULL
       AND NEW.bound_reply IS NOT DISTINCT FROM OLD.bound_reply
       AND NEW.observed_reply IS NOT DISTINCT FROM OLD.observed_reply
       AND NEW.retired_epoch IS NOT DISTINCT FROM OLD.retired_epoch
       AND NEW.endpoint='{}'::jsonb AND NEW.signing_key=''
       AND NEW.registry_snapshot='{}'::jsonb
       AND NEW.transport_snapshot='{}'::jsonb
       AND (v_new - ARRAY[
           'endpoint','signing_key','registry_snapshot','transport_snapshot'
       ]) = (v_old - ARRAY[
           'endpoint','signing_key','registry_snapshot','transport_snapshot'
       ]) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING ERRCODE='55000',
        MESSAGE='video restore context transition is invalid';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._video_restore_context_guard() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_video_restore_context_guard
BEFORE INSERT OR UPDATE OR DELETE
ON aap_inference_routing_v1.video_restore_contexts
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._video_restore_context_guard();
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1._video_restore_immutable()
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
    v_restore_id uuid;
    v_phase text;
BEGIN
    IF TG_OP='INSERT' THEN
        v_restore_id := NEW.restore_id;
    ELSE
        v_restore_id := OLD.restore_id;
    END IF;
    SELECT record.phase INTO v_phase
    FROM aap_inference_routing_v1.video_restore_records AS record
    WHERE record.restore_id=v_restore_id;
    IF TG_OP='INSERT' AND v_phase='prepared' THEN RETURN NEW; END IF;
    RAISE EXCEPTION USING ERRCODE='55000',
        MESSAGE='video restore evidence is immutable';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._video_restore_immutable() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_video_restore_scope_snapshot_immutable
BEFORE INSERT OR UPDATE OR DELETE
ON aap_inference_routing_v1.video_restore_scopes
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._video_restore_immutable();
-- aap-statement-break
CREATE TRIGGER aap_video_restore_attempt_snapshot_immutable
BEFORE INSERT OR UPDATE OR DELETE
ON aap_inference_routing_v1.video_restore_attempts
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._video_restore_immutable();
-- aap-statement-break
CREATE TRIGGER aap_video_restore_no_scope_immutable
BEFORE INSERT OR UPDATE OR DELETE
ON aap_inference_routing_v1.video_restore_no_scope_receipts
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._video_restore_immutable();
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1._video_restore_scope_retirement_guard()
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
    IF TG_OP='INSERT'
       AND EXISTS (
           SELECT 1 FROM aap_inference_routing_v1.video_restore_records AS record
           WHERE record.restore_id=NEW.restore_id AND record.phase='prepared'
       ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING ERRCODE='55000',
        MESSAGE='video restore retirement proof is immutable';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._video_restore_scope_retirement_guard() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_video_restore_scope_retirement_guard
BEFORE INSERT OR UPDATE OR DELETE
ON aap_inference_routing_v1.video_restore_scope_retirements
FOR EACH ROW EXECUTE FUNCTION
    aap_inference_routing_v1._video_restore_scope_retirement_guard();
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1._video_restore_scope_retired(p_scope_id uuid)
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
    SELECT p_scope_id IS NOT NULL AND EXISTS (
        SELECT 1
        FROM aap_inference_routing_v1.video_restore_scope_retirements AS proof
        JOIN aap_inference_routing_v1.video_restore_records AS record
          ON record.restore_id=proof.restore_id
        JOIN aap_inference_routing_v1.video_restore_contexts AS context
          ON context.restore_id=proof.restore_id
         AND context.registry_id=proof.registry_id
        JOIN aap_inference_routing_v1.scopes AS scope
          ON scope.id=proof.scope_id
        WHERE proof.scope_id=p_scope_id
          AND record.phase='completed'
          AND proof.source_routing_realm=record.source_routing_realm
          AND proof.target_routing_realm=aap_inference_routing_v1._routing_realm()
          AND proof.target_routing_realm=record.target_routing_realm
          AND scope.realm=proof.scope_realm
          AND scope.authority_epoch=proof.original_epoch
          AND scope.registry_id=proof.registry_id
          AND scope.state='reconciled' AND scope.closed IS TRUE
          AND context.phase='bound'
          AND context.control_id=proof.control_id
          AND context.new_epoch=proof.new_epoch
          AND context.retired_epoch=proof.retired_epoch
          AND context.backend_incarnation=proof.backend_incarnation
          AND context.challenge=proof.challenge
          AND context.transport_identity_sha256=proof.transport_identity_sha256
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._video_restore_scope_retired(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1.video_execution_scope_retired(
    p_scope_id uuid
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
        FROM aap_inference_routing_v1.scope_retirements AS retirement
        JOIN aap_inference_routing_v1.scopes AS scope
          ON scope.id=retirement.scope_id
        WHERE p_scope_id IS NOT NULL
          AND retirement.scope_id=p_scope_id
          AND retirement.phase='completed'
          AND retirement.realm=aap_inference_routing_v1._routing_realm()
          AND scope.state='reconciled'
    ) OR aap_inference_routing_v1._video_restore_scope_retired(p_scope_id)
$function$;
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
    SELECT (
        EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.command_receipts AS receipt
            WHERE receipt.id=p_request_id AND receipt.command_sha256=p_digest
              AND receipt.operation='video_acquire'
              AND receipt.realm=aap_inference_routing_v1._routing_realm()
              AND aap_inference_routing_v1._routing_exact_keys(
                  receipt.result,ARRAY['reason']
              )
              AND pg_catalog.jsonb_typeof(receipt.result -> 'reason')='string'
        )
        AND NOT EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.scopes WHERE id=p_scope_id
        )
        AND NOT EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.video_execution_scope_permits
            WHERE scope_id=p_scope_id
        )
    ) OR (
        EXISTS (
            SELECT 1
            FROM aap_inference_routing_v1.video_restore_no_scope_receipts AS proof
            JOIN aap_inference_routing_v1.video_restore_records AS record
              ON record.restore_id=proof.restore_id
            JOIN aap_inference_routing_v1.command_receipts AS receipt
              ON receipt.id=proof.request_id
            WHERE proof.scope_id=p_scope_id
              AND proof.request_id=p_request_id
              AND proof.command_sha256=p_digest
              AND record.phase='completed'
              AND proof.source_routing_realm=record.source_routing_realm
              AND proof.target_routing_realm=record.target_routing_realm
              AND proof.target_routing_realm=aap_inference_routing_v1._routing_realm()
              AND receipt.command_sha256=p_digest
              AND receipt.operation='video_acquire'
              AND receipt.realm=proof.receipt_realm
              AND aap_inference_routing_v1._routing_exact_keys(
                  receipt.result,ARRAY['reason']
              )
              AND pg_catalog.jsonb_typeof(receipt.result -> 'reason')='string'
        )
        AND NOT EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.scopes WHERE id=p_scope_id
        )
        AND NOT EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.video_execution_scope_permits
            WHERE scope_id=p_scope_id
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._video_execution_no_scope_rejection(uuid,uuid,text)
    FROM PUBLIC;
-- Jobs computes and owns route-command rekey facts; no caller can read table data.
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1._video_restore_route_snapshot(
    p_secret bytea,
    p_source_realm text
)
RETURNS jsonb
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
    v_intent record;
    v_execution aap_jobs_v1.video_executions%ROWTYPE;
    v_command jsonb;
    v_command_text text;
    v_route_generation bigint;
    v_routes jsonb := '[]'::jsonb;
    v_registry_id uuid;
BEGIN
    IF p_secret IS NULL OR pg_catalog.octet_length(p_secret) <> 32
       OR p_source_realm IS NULL THEN
        RETURN pg_catalog.jsonb_build_object('ok',false);
    END IF;
    IF EXISTS (
        SELECT 1 FROM aap_jobs_v1.video_executions AS execution
        WHERE execution.realm IS DISTINCT FROM p_source_realm
           OR (execution.route_request_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM aap_jobs_v1.video_execution_route_intents AS intent
                WHERE intent.request_id=execution.route_request_id
                  AND intent.job_id=execution.job_id
                  AND intent.run_id=execution.run_id
                  AND intent.generation<=execution.generation
           ))
    ) THEN
        RETURN pg_catalog.jsonb_build_object('ok',false);
    END IF;
    FOR v_intent IN
        SELECT intent.*
        FROM aap_jobs_v1.video_execution_route_intents AS intent
        ORDER BY intent.request_id
    LOOP
        IF v_intent.realm IS DISTINCT FROM p_source_realm
           OR v_intent.command_sha256 !~ '^[0-9a-f]{64}$' THEN
            RETURN pg_catalog.jsonb_build_object('ok',false);
        END IF;
        SELECT execution.* INTO v_execution
        FROM aap_jobs_v1.video_executions AS execution
        WHERE execution.job_id=v_intent.job_id;
        IF NOT FOUND OR v_execution.realm IS DISTINCT FROM p_source_realm
           OR v_intent.run_id IS DISTINCT FROM v_execution.run_id
           OR v_intent.generation > v_execution.generation THEN
            RETURN pg_catalog.jsonb_build_object('ok',false);
        END IF;
        BEGIN
            v_command_text := public.pgp_sym_decrypt(
                v_intent.command_ciphertext,pg_catalog.encode(p_secret,'hex')
            );
            IF v_command_text IS NULL
               OR pg_catalog.octet_length(v_command_text)>65536 THEN
                RETURN pg_catalog.jsonb_build_object('ok',false);
            END IF;
            v_command := v_command_text::jsonb;
            v_route_generation := (v_command #>> '{body,execution,generation}')::bigint;
            v_registry_id := CASE
                WHEN v_command #> '{body,registry_id}' = 'null'::jsonb THEN NULL
                ELSE aap_jobs_v1._video_execution_uuid(
                    v_command #> '{body,registry_id}'
                )
            END;
        EXCEPTION WHEN OTHERS THEN
            RETURN pg_catalog.jsonb_build_object('ok',false);
        END;
        IF pg_catalog.jsonb_typeof(v_command) IS DISTINCT FROM 'object'
           OR NOT aap_jobs_v1._video_execution_exact_keys(
                v_command,ARRAY['schema','request_id','operation','body']
           )
           OR v_command ->> 'schema' IS DISTINCT FROM 'aap-inference-routing/v1'
           OR v_command ->> 'request_id' IS DISTINCT FROM v_intent.request_id::text
           OR v_command ->> 'operation' IS DISTINCT FROM 'video_acquire'
           OR pg_catalog.jsonb_typeof(v_command -> 'body') IS DISTINCT FROM 'object'
           OR NOT aap_jobs_v1._video_execution_exact_keys(
                v_command -> 'body',ARRAY[
                    'pool_id','registry_id','project_id','mode','required_instance_id',
                    'owner','operation','execution'
                ]
           )
           OR NOT aap_jobs_v1._video_execution_exact_keys(
                v_command #> '{body,execution}',
                ARRAY['job_id','run_id','worker_id','generation','lease_token']
           )
           OR aap_jobs_v1._video_execution_uuid(
                v_command #> '{body,execution,job_id}'
              ) IS DISTINCT FROM v_intent.job_id
           OR aap_jobs_v1._video_execution_uuid(
                v_command #> '{body,execution,run_id}'
              ) IS DISTINCT FROM v_intent.run_id
           OR aap_jobs_v1._video_execution_uuid(
                v_command #> '{body,execution,worker_id}'
              ) IS NULL
           OR v_route_generation IS DISTINCT FROM v_intent.generation
           OR aap_jobs_v1._video_execution_uuid(
                v_command #> '{body,execution,lease_token}'
              ) IS NULL
           OR v_command #>> '{body,owner}' IS DISTINCT FROM
                'tracker:' || v_intent.job_id::text
           OR v_command #>> '{body,operation}' IS DISTINCT FROM 'tracker'
           OR pg_catalog.encode(public.digest(pg_catalog.convert_to(
                aap_inference_routing_v1._video_execution_canonical_json(
                    v_command - 'request_id'
                ),'UTF8'
              ),'sha256'),'hex') IS DISTINCT FROM v_intent.command_sha256 THEN
            RETURN pg_catalog.jsonb_build_object('ok',false);
        END IF;
        v_routes := v_routes || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'request_id',v_intent.request_id,
                'job_id',v_intent.job_id,
                'run_id',v_intent.run_id,
                'generation',v_intent.generation,
                'scope_id',v_intent.scope_id,
                'command_sha256',v_intent.command_sha256,
                'state',v_intent.state,
                'registry_id',v_registry_id
            )
        );
    END LOOP;
    RETURN pg_catalog.jsonb_build_object('ok',true,'routes',v_routes);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_jobs_v1._video_restore_route_snapshot(bytea,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.video_restore_authority(
    p_action text,
    p_arguments jsonb
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
    v_authority aap_jobs_v1.video_execution_authority%ROWTYPE;
    v_execution aap_jobs_v1.video_executions%ROWTYPE;
    v_intent aap_jobs_v1.video_execution_route_intents%ROWTYPE;
    v_current record;
    v_source_realm text;
    v_target_realm text;
    v_secret_sha text;
    v_target_oid oid;
    v_target_name text;
    v_target_cluster bigint;
    v_routes jsonb;
    v_route_snapshot jsonb;
    v_command jsonb;
    v_command_text text;
    v_new_secret bytea;
    v_execution_count bigint;
    v_route_count bigint;
    v_row record;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_failure text;
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','unsupported_isolation');
    END IF;
    IF p_action NOT IN ('inspect','prepare','finish')
       OR pg_catalog.jsonb_typeof(p_arguments) IS DISTINCT FROM 'object'
       OR pg_catalog.octet_length(p_arguments::text)>131072 THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_request');
    END IF;
    IF p_action='inspect' THEN
        IF NOT aap_jobs_v1._video_execution_exact_keys(p_arguments,ARRAY[]::text[]) THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_request');
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock_shared(
            pg_catalog.hashtextextended(
                'aap-native-authority-fence/v1:' || pg_catalog.current_database(),284
            )
        );
    ELSE
        IF NOT aap_jobs_v1._video_execution_exact_keys(
            p_arguments,ARRAY[
                'restore_id','source_jobs_realm','target_jobs_realm',
                'source_jobs_secret_sha256'
            ]
        ) THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_request');
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'aap-native-authority-fence/v1:' || pg_catalog.current_database(),284
            )
        );
    END IF;
    SELECT authority.* INTO v_authority
    FROM aap_jobs_v1.video_execution_authority AS authority
    WHERE authority.singleton IS TRUE;
    IF NOT FOUND OR pg_catalog.octet_length(v_authority.secret)<>32 THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','authority_unavailable');
    END IF;
    SELECT database.oid AS database_oid,pg_catalog.current_database() AS database_name,
           control.system_identifier AS cluster_identifier
    INTO v_current
    FROM pg_catalog.pg_database AS database
    CROSS JOIN LATERAL pg_catalog.pg_control_system() AS control
    WHERE database.datname=pg_catalog.current_database();
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','authority_unavailable');
    END IF;
    v_source_realm := v_authority.database_oid::text || ':' ||
        v_authority.database_name || ':' || v_authority.cluster_identifier::text;
    v_target_oid := v_current.database_oid;
    v_target_name := v_current.database_name;
    v_target_cluster := v_current.cluster_identifier;
    v_target_realm := v_target_oid::text || ':' || v_target_name || ':' ||
        v_target_cluster::text;
    v_secret_sha := pg_catalog.encode(
        public.digest(v_authority.secret,'sha256'),'hex'
    );
    IF p_action='inspect' THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok',true,
            'identity',pg_catalog.jsonb_build_object(
                'source_jobs_realm',v_source_realm,
                'target_jobs_realm',v_target_realm,
                'source_jobs_secret_sha256',v_secret_sha,
                'source_jobs_oid',v_authority.database_oid::text,
                'source_jobs_name',v_authority.database_name,
                'target_jobs_oid',v_target_oid::text,
                'target_jobs_name',v_target_name
            )
        );
    END IF;
    BEGIN
        PERFORM aap_jobs_v1._video_execution_uuid(p_arguments -> 'restore_id');
    EXCEPTION WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_request');
    END;
    IF pg_catalog.jsonb_typeof(p_arguments -> 'source_jobs_realm') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_arguments -> 'target_jobs_realm') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_arguments -> 'source_jobs_secret_sha256')
            IS DISTINCT FROM 'string'
       OR NOT aap_jobs_v1._video_execution_exact_keys(
            p_arguments,ARRAY[
                'restore_id','source_jobs_realm','target_jobs_realm',
                'source_jobs_secret_sha256'
            ]
       ) THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_request');
    END IF;
    IF p_arguments ->> 'source_jobs_realm' IS DISTINCT FROM v_source_realm
       OR p_arguments ->> 'source_jobs_secret_sha256' IS DISTINCT FROM v_secret_sha THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','source_mismatch');
    END IF;
    IF p_arguments ->> 'target_jobs_realm' IS DISTINCT FROM v_target_realm THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','realm_mismatch');
    END IF;
    IF p_action='prepare' THEN
        IF EXISTS (SELECT 1 FROM aap_jobs_v1.video_restore_fence) THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'reason','restore_busy');
        END IF;
        v_routes := aap_jobs_v1._video_restore_route_snapshot(
            v_authority.secret,v_source_realm
        );
        IF v_routes -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'reason','route_snapshot_invalid');
        END IF;
        INSERT INTO aap_jobs_v1.video_restore_fence(
            singleton,restore_id,source_jobs_realm,target_jobs_realm,
            source_jobs_secret_sha256,route_snapshot
        ) VALUES (
            true,(p_arguments ->> 'restore_id')::uuid,v_source_realm,v_target_realm,
            v_secret_sha,v_routes -> 'routes'
        );
        RETURN pg_catalog.jsonb_build_object(
            'ok',true,'routes',v_routes -> 'routes'
        );
    END IF;
    IF p_action='finish' THEN
        SELECT fence.* INTO v_current
        FROM aap_jobs_v1.video_restore_fence AS fence
        WHERE fence.singleton IS TRUE AND
              fence.restore_id=(p_arguments ->> 'restore_id')::uuid
        FOR UPDATE NOWAIT;
        IF NOT FOUND
           OR v_current.source_jobs_realm IS DISTINCT FROM v_source_realm
           OR v_current.target_jobs_realm IS DISTINCT FROM v_target_realm
           OR v_current.source_jobs_secret_sha256 IS DISTINCT FROM v_secret_sha THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'reason','restore_conflict');
        END IF;
        v_routes := aap_jobs_v1._video_restore_route_snapshot(
            v_authority.secret,v_source_realm
        );
        IF v_routes -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_routes -> 'routes' IS DISTINCT FROM v_current.route_snapshot THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'reason','route_snapshot_invalid');
        END IF;
        FOR v_row IN
            SELECT execution.job_id
            FROM aap_jobs_v1.video_executions AS execution
            ORDER BY execution.job_id
            FOR UPDATE NOWAIT
        LOOP
            NULL;
        END LOOP;
        FOR v_row IN
            SELECT intent.request_id
            FROM aap_jobs_v1.video_execution_route_intents AS intent
            ORDER BY intent.request_id
            FOR UPDATE NOWAIT
        LOOP
            NULL;
        END LOOP;
        SELECT authority.* INTO v_authority
        FROM aap_jobs_v1.video_execution_authority AS authority
        WHERE authority.singleton IS TRUE
        FOR UPDATE NOWAIT;
        IF NOT FOUND
           OR v_authority.database_oid::text || ':' || v_authority.database_name || ':' ||
              v_authority.cluster_identifier::text IS DISTINCT FROM v_source_realm THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'reason','source_mismatch');
        END IF;
        v_execution_count := (SELECT pg_catalog.count(*)
                              FROM aap_jobs_v1.video_executions);
        v_route_count := pg_catalog.jsonb_array_length(v_current.route_snapshot);
        v_new_secret := public.gen_random_bytes(32);
        FOR v_intent IN
            SELECT intent.*
            FROM aap_jobs_v1.video_execution_route_intents AS intent
            ORDER BY intent.request_id
        LOOP
            v_command_text := public.pgp_sym_decrypt(
                v_intent.command_ciphertext,pg_catalog.encode(v_authority.secret,'hex')
            );
            v_command := v_command_text::jsonb;
            UPDATE aap_jobs_v1.video_execution_route_intents AS intent
            SET command_ciphertext=public.pgp_sym_encrypt(
                    v_command_text,pg_catalog.encode(v_new_secret,'hex'),
                    'cipher-algo=aes256'
                ),
                realm=v_target_realm,
                updated_at=v_now
            WHERE intent.request_id=v_intent.request_id;
        END LOOP;
        UPDATE aap_jobs_v1.video_executions AS execution
        SET lease_token_hash=public.gen_random_bytes(32),
            lease_expires_at=v_now,
            state=CASE WHEN execution.route_request_id IS NULL
                       THEN execution.state ELSE 'recovery_required' END,
            realm=v_target_realm,
            updated_at=v_now;
        UPDATE aap_jobs_v1.video_execution_authority AS authority
        SET secret=v_new_secret,database_oid=v_target_oid,
            database_name=v_target_name,cluster_identifier=v_target_cluster
        WHERE authority.singleton IS TRUE;
        DELETE FROM aap_jobs_v1.video_restore_fence
        WHERE singleton IS TRUE AND
              restore_id=(p_arguments ->> 'restore_id')::uuid;
        RETURN pg_catalog.jsonb_build_object(
            'ok',true,
            'executions_invalidated',v_execution_count,
            'route_intents_rewrapped',v_route_count
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_request');
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','execution_busy');
    WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','storage_unavailable');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_jobs_v1.video_restore_authority(text,jsonb) FROM PUBLIC;
-- Retain the original Native scope flow for all non-status actions. The status
-- path below validates current Jobs proof itself and never mints a capability
-- for a scope retired into another routing realm.
-- aap-statement-break
DO $clone_video_scope_step$
DECLARE
    v_definition text;
    v_clone text;
    v_owner text;
BEGIN
    SELECT restore.function_definition,role.rolname
    INTO v_definition,v_owner
    FROM aap_inference_routing_v1.video_restore_function_restore AS restore
    JOIN pg_catalog.pg_proc AS function
      ON function.oid=pg_catalog.to_regprocedure(restore.signature)::oid
    JOIN pg_catalog.pg_roles AS role ON role.oid=function.proowner
    WHERE restore.signature=
        'aap_inference_routing_v1.video_execution_scope_step(text,text)';
    v_clone := pg_catalog.replace(
        v_definition,
        'FUNCTION aap_inference_routing_v1.video_execution_scope_step(',
        'FUNCTION aap_inference_routing_v1._video_execution_scope_step_pre_restore('
    );
    IF v_definition IS NULL OR v_clone IS NOT DISTINCT FROM v_definition THEN
        RAISE EXCEPTION USING ERRCODE='55000',
            MESSAGE='cannot preserve Native video scope status implementation';
    END IF;
    EXECUTE v_clone;
    EXECUTE pg_catalog.format(
        'ALTER FUNCTION aap_inference_routing_v1._video_execution_scope_step_pre_restore(text,text) OWNER TO %I',
        v_owner
    );
    REVOKE ALL ON FUNCTION
        aap_inference_routing_v1._video_execution_scope_step_pre_restore(text,text)
        FROM PUBLIC;
END
$clone_video_scope_step$;
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
    v_proof jsonb;
    v_jobs_reply jsonb;
    v_command jsonb;
    v_intent jsonb;
    v_scope jsonb;
    v_scope_row record;
    v_job_id uuid;
    v_run_id uuid;
    v_worker_id uuid;
    v_token uuid;
    v_generation bigint;
    v_scope_id uuid;
    v_registry_id uuid;
    v_request_id uuid;
    v_digest text;
    v_command_generation bigint;
    v_route_secret bytea;
BEGIN
    IF p_action IS DISTINCT FROM 'status' THEN
        RETURN aap_inference_routing_v1._video_execution_scope_step_pre_restore(
            p_action,p_arguments
        );
    END IF;
    IF p_arguments IS NULL OR pg_catalog.octet_length(p_arguments)>2097152 THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'invalid_command');
    END IF;
    BEGIN
        v_args := p_arguments::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'invalid_command');
    END;
    IF NOT aap_inference_routing_v1._routing_exact_keys(
        v_args,ARRAY['job_id','run_id','worker_id','generation','lease_token']
    ) THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'invalid_command');
    END IF;
    BEGIN
        v_job_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'job_id');
        v_run_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'run_id');
        v_worker_id := aap_inference_routing_v1._routing_uuid(v_args ->> 'worker_id');
        v_token := aap_inference_routing_v1._routing_uuid(v_args ->> 'lease_token');
        v_generation := aap_inference_routing_v1._routing_integer(
            v_args -> 'generation',1,9223372036854775807
        );
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'invalid_command');
    END;
    IF v_generation IS NULL THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'invalid_command');
    END IF;
    v_route_secret := aap_inference_routing_v1._routing_secret();
    IF v_route_secret IS NULL THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'realm_mismatch');
    END IF;
    v_proof := pg_catalog.jsonb_build_object(
        'job_id',v_job_id,'run_id',v_run_id,'worker_id',v_worker_id,
        'generation',v_generation,'lease_token',v_token
    );
    v_jobs_reply := aap_jobs_v1._video_execution_route_step('status',v_proof::text);
    IF v_jobs_reply IS NULL OR v_jobs_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_inference_routing_v1._video_execution_result(
            NULL,COALESCE(v_jobs_reply ->> 'reason','scope_unavailable')
        );
    END IF;
    v_intent := v_jobs_reply -> 'intent';
    IF v_intent IS NULL OR v_intent='null'::jsonb THEN
        RETURN aap_inference_routing_v1._video_execution_result(
            pg_catalog.jsonb_build_object('intent',NULL,'scope',NULL)
        );
    END IF;
    IF v_intent ->> 'job_id' IS DISTINCT FROM v_job_id::text
       OR v_intent ->> 'run_id' IS DISTINCT FROM v_run_id::text
       OR v_intent ->> 'state' NOT IN ('prepared','bound') THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_unavailable');
    END IF;
    BEGIN
        v_scope_id := aap_inference_routing_v1._routing_uuid(
            v_intent ->> 'scope_id'
        );
        v_request_id := aap_inference_routing_v1._routing_uuid(
            v_intent ->> 'request_id'
        );
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_unavailable');
    END;
    v_digest := v_intent ->> 'command_sha256';
    IF NOT aap_inference_routing_v1._video_restore_digest_valid(v_digest) THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_unavailable');
    END IF;
    v_command := v_jobs_reply -> 'command';
    BEGIN
        v_registry_id := CASE
            WHEN v_command #> '{body,registry_id}' = 'null'::jsonb THEN NULL
            ELSE aap_inference_routing_v1._routing_uuid(
                v_command #>> '{body,registry_id}'
            )
        END;
        v_command_generation :=
            (v_command #>> '{body,execution,generation}')::bigint;
        PERFORM aap_inference_routing_v1._routing_uuid(
            v_command #>> '{body,execution,job_id}'
        );
        PERFORM aap_inference_routing_v1._routing_uuid(
            v_command #>> '{body,execution,run_id}'
        );
        PERFORM aap_inference_routing_v1._routing_uuid(
            v_command #>> '{body,execution,worker_id}'
        );
        PERFORM aap_inference_routing_v1._routing_uuid(
            v_command #>> '{body,execution,lease_token}'
        );
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_unavailable');
    END;
    IF NOT aap_inference_routing_v1._routing_exact_keys(
            v_command,ARRAY['schema','request_id','operation','body']
       )
       OR v_command ->> 'schema' IS DISTINCT FROM 'aap-inference-routing/v1'
       OR v_command ->> 'request_id' IS DISTINCT FROM v_request_id::text
       OR v_command ->> 'operation' IS DISTINCT FROM 'video_acquire'
       OR NOT aap_inference_routing_v1._routing_exact_keys(
            v_command -> 'body',ARRAY[
                'pool_id','registry_id','project_id','mode','required_instance_id',
                'owner','operation','execution'
            ]
       )
       OR NOT aap_inference_routing_v1._routing_exact_keys(
            v_command #> '{body,execution}',ARRAY[
                'job_id','run_id','worker_id','generation','lease_token'
            ]
       )
       OR v_command #>> '{body,execution,job_id}' IS DISTINCT FROM v_job_id::text
       OR v_command #>> '{body,execution,run_id}' IS DISTINCT FROM v_run_id::text
       OR v_command_generation IS DISTINCT FROM (v_intent ->> 'generation')::bigint
       OR v_command #>> '{body,owner}' IS DISTINCT FROM 'tracker:' || v_job_id::text
       OR v_command #>> '{body,operation}' IS DISTINCT FROM 'tracker'
       OR pg_catalog.encode(public.digest(pg_catalog.convert_to(
            aap_inference_routing_v1._video_execution_canonical_json(
                v_command - 'request_id'
            ),'UTF8'
          ),'sha256'),'hex') IS DISTINCT FROM v_digest THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_unavailable');
    END IF;
    IF v_intent ->> 'state'='prepared' THEN
        IF EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.scopes AS scope
            WHERE scope.id=v_scope_id
        ) OR NOT aap_inference_routing_v1._video_execution_no_scope_rejection(
            v_scope_id,v_request_id,v_digest
        ) THEN
            RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_unavailable');
        END IF;
        v_scope := NULL;
    ELSE
        SELECT scope.id,scope.registry_id,scope.command_sha256,scope.owner,
               scope.operation,scope.realm
        INTO v_scope_row
        FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.id=v_scope_id;
        IF NOT FOUND
           OR v_scope_row.command_sha256 IS DISTINCT FROM v_digest
           OR v_scope_row.owner IS DISTINCT FROM 'tracker:' || v_job_id::text
           OR v_scope_row.operation IS DISTINCT FROM 'tracker'
           OR (v_registry_id IS NOT NULL
               AND v_scope_row.registry_id IS DISTINCT FROM v_registry_id) THEN
            RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_unavailable');
        END IF;
        IF aap_inference_routing_v1._video_restore_scope_retired(v_scope_id) THEN
            v_scope := NULL;
        ELSIF v_scope_row.realm IS DISTINCT FROM
              aap_inference_routing_v1._routing_realm() THEN
            RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_unavailable');
        ELSE
            v_scope := aap_inference_routing_v1._routing_scope_view(v_scope_id,true);
            IF v_scope IS NULL OR v_scope ->> 'id' IS DISTINCT FROM v_scope_id::text
               OR v_scope ->> 'registry_id' IS DISTINCT FROM v_scope_row.registry_id::text THEN
                RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_unavailable');
            END IF;
        END IF;
    END IF;
    RETURN aap_inference_routing_v1._video_execution_result(
        pg_catalog.jsonb_build_object('intent',v_intent,'scope',v_scope)
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'management_busy');
    WHEN OTHERS THEN
        RETURN aap_inference_routing_v1._video_execution_result(NULL,'scope_unavailable');
END
$function$;
-- aap-statement-break
-- The sole operator entrypoint. Each step is a short, closed, READ COMMITTED
-- transaction. Prepare/finish exclude every ordinary Native authority call.
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1._video_restore_registry_snapshot(
    p_registry_id uuid
)
RETURNS jsonb
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
    v_state aap_inference_routing_v1.registry_state%ROWTYPE;
    v_transport jsonb;
BEGIN
    SELECT state.* INTO v_state
    FROM aap_inference_routing_v1.registry_state AS state
    WHERE state.registry_id=p_registry_id;
    IF NOT FOUND THEN RETURN NULL; END IF;
    v_transport := aap_inference_routing_v1._routing_transport_identity(p_registry_id);
    RETURN pg_catalog.jsonb_build_object(
        'registry_id',v_state.registry_id,
        'config_revision',v_state.config_revision,
        'eligibility_revision',v_state.eligibility_revision,
        'authority_epoch',v_state.authority_epoch,
        'backend_incarnation',v_state.backend_incarnation,
        'continuity',v_state.continuity,
        'epoch_key_sha256',pg_catalog.encode(
            public.digest(v_state.epoch_key,'sha256'),'hex'
        ),
        'realm',v_state.realm,
        'updated_at',v_state.updated_at,
        'transport_identity_sha256',CASE WHEN v_transport IS NULL THEN NULL ELSE
            pg_catalog.encode(public.digest(pg_catalog.convert_to(
                aap_inference_routing_v1._video_execution_canonical_json(v_transport),
                'UTF8'
            ),'sha256'),'hex') END
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1._video_restore_registry_snapshot(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1.video_restore_step(
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
    v_command jsonb;
    v_command_sha text;
    v_restore_id uuid;
    v_record aap_inference_routing_v1.video_restore_records%ROWTYPE;
    v_context aap_inference_routing_v1.video_restore_contexts%ROWTYPE;
    v_scope aap_inference_routing_v1.scopes%ROWTYPE;
    v_state aap_inference_routing_v1.registry_state%ROWTYPE;
    v_route_authority aap_inference_routing_v1.authority_secret%ROWTYPE;
    v_job_identity jsonb;
    v_job_helper jsonb;
    v_identity jsonb;
    v_route_snapshot jsonb;
    v_registry_states jsonb;
    v_route jsonb;
    v_transport jsonb;
    v_endpoint jsonb;
    v_registry_snapshot jsonb;
    v_reply jsonb;
    v_output jsonb;
    v_context_output jsonb;
    v_current_identity record;
    v_registry_id uuid;
    v_new_epoch uuid;
    v_scope_id uuid;
    v_request_id uuid;
    v_digest text;
    v_phase text;
    v_active_requests bigint;
    v_source_jobs_realm text;
    v_target_jobs_realm text;
    v_source_routing_realm text;
    v_target_routing_realm text;
    v_source_jobs_secret_sha text;
    v_source_routing_secret_sha text;
    v_source_jobs_oid text;
    v_source_jobs_name text;
    v_source_routing_oid oid;
    v_source_routing_name text;
    v_target_jobs_oid text;
    v_target_jobs_name text;
    v_target_routing_oid oid;
    v_target_routing_name text;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_counts jsonb;
    v_job_counts jsonb;
    v_executions_invalidated bigint;
    v_routes_rewrapped bigint;
    v_registries_rebound bigint;
    v_scopes_retired bigint;
    v_attempts_reconciled bigint;
    v_lock record;
    v_receipt record;
    v_current_transport jsonb;
    v_current_registry_snapshot jsonb;
    v_reason text;
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RETURN aap_inference_routing_v1._video_restore_reply(NULL,'unsupported_isolation');
    END IF;
    IF p_action NOT IN (
        'policy','inspect','prepare','status','observe','record_bound','finish'
    ) OR p_arguments IS NULL OR pg_catalog.octet_length(p_arguments)>131072 THEN
        RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
    END IF;
    BEGIN
        v_args := p_arguments::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
    END;
    IF pg_catalog.jsonb_typeof(v_args) IS DISTINCT FROM 'object' THEN
        RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
    END IF;
    IF p_action IN ('policy','inspect') THEN
        IF NOT aap_inference_routing_v1._video_restore_exact_keys(
            v_args,ARRAY[]::text[]
        ) THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
        END IF;
    ELSIF p_action='prepare' THEN
        IF NOT aap_inference_routing_v1._video_restore_exact_keys(
            v_args,ARRAY['command']
        ) OR pg_catalog.jsonb_typeof(v_args -> 'command') IS DISTINCT FROM 'object' THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
        END IF;
    ELSIF p_action IN ('status','finish') THEN
        IF NOT aap_inference_routing_v1._video_restore_exact_keys(
            v_args,ARRAY['restore_id']
        ) THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
        END IF;
    ELSE
        IF NOT aap_inference_routing_v1._video_restore_exact_keys(
            v_args,ARRAY['restore_id','registry_id','reply']
        ) OR pg_catalog.jsonb_typeof(v_args -> 'reply') IS DISTINCT FROM 'object' THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
        END IF;
    END IF;
    IF p_action='prepare' THEN
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'aap-native-authority-fence/v1:' || pg_catalog.current_database(),284
            )
        );
    ELSIF p_action='finish' THEN
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'aap-native-authority-fence/v1:' || pg_catalog.current_database(),284
            )
        );
    ELSE
        PERFORM pg_catalog.pg_advisory_xact_lock_shared(
            pg_catalog.hashtextextended(
                'aap-native-authority-fence/v1:' || pg_catalog.current_database(),284
            )
        );
    END IF;

    IF p_action='policy' THEN
        v_job_identity := aap_jobs_v1.video_restore_authority('inspect','{}'::jsonb);
        SELECT authority.* INTO v_route_authority
        FROM aap_inference_routing_v1.authority_secret AS authority
        WHERE authority.singleton IS TRUE;
        RETURN aap_inference_routing_v1._video_restore_reply(
            pg_catalog.jsonb_build_object(
                'ready',COALESCE(
                    v_job_identity -> 'ok'='true'::jsonb
                    AND FOUND
                    AND pg_catalog.octet_length(v_route_authority.secret)=32,
                    false
                ),
                'functions',pg_catalog.jsonb_build_array(
                    'aap_inference_routing_v1.video_restore_step(text,text)'
                )
            )
        );
    END IF;

    IF p_action IN ('inspect','prepare') THEN
        v_job_identity := aap_jobs_v1.video_restore_authority('inspect','{}'::jsonb);
        IF v_job_identity IS NULL OR v_job_identity -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR pg_catalog.jsonb_typeof(v_job_identity -> 'identity') IS DISTINCT FROM 'object' THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                NULL,'authority_unavailable'
            );
        END IF;
        SELECT authority.* INTO v_route_authority
        FROM aap_inference_routing_v1.authority_secret AS authority
        WHERE authority.singleton IS TRUE;
        IF NOT FOUND OR pg_catalog.octet_length(v_route_authority.secret)<>32 THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                NULL,'authority_unavailable'
            );
        END IF;
        SELECT database.oid AS database_oid,
               pg_catalog.current_database() AS database_name
        INTO v_current_identity
        FROM pg_catalog.pg_database AS database
        WHERE database.datname=pg_catalog.current_database();
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                NULL,'authority_unavailable'
            );
        END IF;
        v_source_routing_oid := v_route_authority.realm_database_oid;
        v_source_routing_name := v_route_authority.realm_database_name;
        v_target_routing_oid := v_current_identity.database_oid;
        v_target_routing_name := v_current_identity.database_name;
        v_source_routing_realm := v_source_routing_oid::text || ':' ||
            v_source_routing_name;
        v_target_routing_realm := v_target_routing_oid::text || ':' ||
            v_target_routing_name;
        v_source_routing_secret_sha := pg_catalog.encode(
            public.digest(v_route_authority.secret,'sha256'),'hex'
        );
        v_source_jobs_realm := v_job_identity #>> '{identity,source_jobs_realm}';
        v_target_jobs_realm := v_job_identity #>> '{identity,target_jobs_realm}';
        v_source_jobs_secret_sha := v_job_identity #>> '{identity,source_jobs_secret_sha256}';
        v_source_jobs_oid := v_job_identity #>> '{identity,source_jobs_oid}';
        v_source_jobs_name := v_job_identity #>> '{identity,source_jobs_name}';
        v_target_jobs_oid := v_job_identity #>> '{identity,target_jobs_oid}';
        v_target_jobs_name := v_job_identity #>> '{identity,target_jobs_name}';
        v_identity := pg_catalog.jsonb_build_object(
            'source_jobs_realm',v_source_jobs_realm,
            'target_jobs_realm',v_target_jobs_realm,
            'source_routing_realm',v_source_routing_realm,
            'target_routing_realm',v_target_routing_realm,
            'source_jobs_secret_sha256',v_source_jobs_secret_sha,
            'source_routing_secret_sha256',v_source_routing_secret_sha
        );
        IF p_action='inspect' THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                pg_catalog.jsonb_build_object('identity',v_identity)
            );
        END IF;
    END IF;

    IF p_action='prepare' THEN
        v_command := v_args -> 'command';
        IF NOT aap_inference_routing_v1._video_restore_exact_keys(
            v_command,ARRAY[
                'schema','restore_id','backup_sha256','fence_sha256',
                'source_jobs_realm','target_jobs_realm','source_routing_realm',
                'target_routing_realm','source_jobs_secret_sha256',
                'source_routing_secret_sha256'
            ]
        ) OR v_command - ARRAY[
            'schema','restore_id','backup_sha256','fence_sha256',
            'source_jobs_realm','target_jobs_realm','source_routing_realm',
            'target_routing_realm','source_jobs_secret_sha256',
            'source_routing_secret_sha256'
        ] <> '{}'::jsonb THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_each(v_command) AS item(key,value)
            WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
        ) THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
        END IF;
        BEGIN
            v_restore_id := aap_inference_routing_v1._video_restore_uuid(
                v_command -> 'restore_id'
            );
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
        END;
        IF v_command ->> 'schema' IS DISTINCT FROM 'aap-video-restore/v1'
           OR NOT aap_inference_routing_v1._video_restore_digest_valid(
                v_command ->> 'backup_sha256'
           )
           OR NOT aap_inference_routing_v1._video_restore_digest_valid(
                v_command ->> 'fence_sha256'
           )
           OR NOT aap_inference_routing_v1._video_restore_digest_valid(
                v_command ->> 'source_jobs_secret_sha256'
           )
           OR NOT aap_inference_routing_v1._video_restore_digest_valid(
                v_command ->> 'source_routing_secret_sha256'
           ) THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
        END IF;
        v_command_sha := aap_inference_routing_v1._video_restore_command_sha256(v_command);
        SELECT record.* INTO v_record
        FROM aap_inference_routing_v1.video_restore_records AS record
        WHERE record.restore_id=v_restore_id;
        IF FOUND THEN
            IF v_record.command_sha256 IS DISTINCT FROM v_command_sha
               OR v_record.command IS DISTINCT FROM v_command THEN
                RETURN aap_inference_routing_v1._video_restore_reply(NULL,'restore_conflict');
            END IF;
            v_output := aap_inference_routing_v1._video_restore_reply(
                pg_catalog.jsonb_build_object(
                    'restore',aap_inference_routing_v1._video_restore_record(v_restore_id),
                    'contexts',aap_inference_routing_v1._video_restore_contexts(v_restore_id)
                )
            );
            IF pg_catalog.octet_length(v_output::text)>65536 THEN
                RETURN aap_inference_routing_v1._video_restore_reply(
                    NULL,'storage_unavailable'
                );
            END IF;
            RETURN v_output;
        END IF;
        IF EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.video_restore_records AS record
            WHERE record.phase='prepared'
        ) THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'restore_busy');
        END IF;
        IF v_command ->> 'source_jobs_realm' IS DISTINCT FROM v_source_jobs_realm
           OR v_command ->> 'source_jobs_secret_sha256'
                IS DISTINCT FROM v_source_jobs_secret_sha
           OR v_command ->> 'source_routing_realm'
                IS DISTINCT FROM v_source_routing_realm
           OR v_command ->> 'source_routing_secret_sha256'
                IS DISTINCT FROM v_source_routing_secret_sha THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'source_mismatch');
        END IF;
        IF v_command ->> 'target_jobs_realm' IS DISTINCT FROM v_target_jobs_realm
           OR v_command ->> 'target_routing_realm' IS DISTINCT FROM v_target_routing_realm THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'realm_mismatch');
        END IF;
        IF v_source_jobs_oid IS DISTINCT FROM v_route_authority.realm_database_oid::text
           OR v_source_jobs_name IS DISTINCT FROM v_route_authority.realm_database_name
           OR v_target_jobs_oid IS DISTINCT FROM v_target_routing_oid::text
           OR v_target_jobs_name IS DISTINCT FROM v_target_routing_name THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'source_mismatch');
        END IF;
        IF EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.controls AS control
            WHERE control.phase IN ('prepared','draining','uncertain')
        ) OR EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.control_attempts AS attempt
            WHERE attempt.state IN ('active','uncertain')
        ) OR EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.scope_retirements AS retirement
            WHERE retirement.phase IN ('prepared','draining')
        ) THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'restore_busy');
        END IF;
        IF EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.registry_state AS state
            WHERE state.realm IS DISTINCT FROM v_source_routing_realm
               OR pg_catalog.octet_length(state.epoch_key)<>32
        ) THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                NULL,'authority_unavailable'
            );
        END IF;
        IF EXISTS (
            SELECT 1
            FROM aap_inference_routing_v1.scopes AS scope
            LEFT JOIN aap_inference_routing_v1.registry_state AS state
              ON state.registry_id=scope.registry_id
            WHERE state.registry_id IS NULL
               OR state.continuity IS DISTINCT FROM 'ready'
               OR state.backend_incarnation IS NULL
        ) THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                NULL,'registry_unavailable'
            );
        END IF;

        SELECT COALESCE(pg_catalog.jsonb_agg(
            aap_inference_routing_v1._video_restore_registry_snapshot(state.registry_id)
            ORDER BY state.registry_id
        ),'[]'::jsonb)
        INTO v_registry_states
        FROM aap_inference_routing_v1.registry_state AS state;

        INSERT INTO aap_inference_routing_v1.video_restore_records(
            restore_id,command,command_sha256,backup_sha256,fence_sha256,
            source_jobs_realm,target_jobs_realm,source_routing_realm,
            target_routing_realm,source_jobs_secret_sha256,
            source_routing_secret_sha256,registry_state_snapshot,phase
        ) VALUES (
            v_restore_id,v_command,v_command_sha,
            v_command ->> 'backup_sha256',v_command ->> 'fence_sha256',
            v_source_jobs_realm,v_target_jobs_realm,v_source_routing_realm,
            v_target_routing_realm,v_source_jobs_secret_sha,
            v_source_routing_secret_sha,v_registry_states,'prepared'
        );
        INSERT INTO aap_inference_routing_v1.video_restore_scopes(
            restore_id,scope_id,registry_id,scope_realm,original_epoch,scope_snapshot
        )
        SELECT v_restore_id,scope.id,scope.registry_id,scope.realm,
               scope.authority_epoch,pg_catalog.to_jsonb(scope)
        FROM aap_inference_routing_v1.scopes AS scope
        ORDER BY scope.id;
        INSERT INTO aap_inference_routing_v1.video_restore_attempts(
            restore_id,scope_id,attempt_id,attempt_snapshot
        )
        SELECT v_restore_id,attempt.scope_id,attempt.id,pg_catalog.to_jsonb(attempt)
        FROM aap_inference_routing_v1.attempts AS attempt
        JOIN aap_inference_routing_v1.video_restore_scopes AS scope
          ON scope.restore_id=v_restore_id AND scope.scope_id=attempt.scope_id
        ORDER BY attempt.id;
        FOR v_registry_id IN
            SELECT state.registry_id
            FROM aap_inference_routing_v1.registry_state AS state
            WHERE state.continuity='ready'
            ORDER BY state.registry_id
        LOOP
            SELECT state.* INTO v_state
            FROM aap_inference_routing_v1.registry_state AS state
            WHERE state.registry_id=v_registry_id;
            v_transport := aap_inference_routing_v1._routing_transport_identity(
                v_registry_id
            );
            IF NOT FOUND OR v_state.realm IS DISTINCT FROM v_source_routing_realm
               OR v_state.continuity IS DISTINCT FROM 'ready'
               OR v_state.backend_incarnation IS NULL
               OR v_transport IS NULL
               OR pg_catalog.jsonb_typeof(v_transport -> 'url') IS DISTINCT FROM 'string'
               OR pg_catalog.char_length(v_transport ->> 'url') NOT BETWEEN 1 AND 4096
               OR (v_transport ->> 'url') !~ '^[^[:space:][:cntrl:]]+$'
               OR v_transport ->> 'auth_method' IS DISTINCT FROM 'token'
               OR pg_catalog.jsonb_typeof(v_transport -> 'auth_token')
                    IS DISTINCT FROM 'string'
               OR pg_catalog.char_length(v_transport ->> 'auth_token') NOT BETWEEN 1 AND 8192
               OR (v_transport ->> 'auth_token') !~ '^[!-~]+$' THEN
                v_reason := 'registry_unavailable';
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='video restore rejected';
            END IF;
            v_endpoint := pg_catalog.jsonb_build_object(
                'url',v_transport -> 'url',
                'auth_method',v_transport -> 'auth_method',
                'auth_token',v_transport -> 'auth_token'
            );
            v_registry_snapshot :=
                aap_inference_routing_v1._video_restore_registry_snapshot(v_registry_id);
            v_new_epoch := public.gen_random_uuid();
            WHILE v_new_epoch=v_state.authority_epoch LOOP
                v_new_epoch := public.gen_random_uuid();
            END LOOP;
            INSERT INTO aap_inference_routing_v1.video_restore_contexts(
                restore_id,control_id,registry_id,phase,new_epoch,challenge,
                retired_epoch,backend_incarnation,endpoint,signing_key,
                transport_identity_sha256,registry_snapshot,transport_snapshot
            ) VALUES (
                v_restore_id,public.gen_random_uuid(),v_registry_id,'prepared',
                v_new_epoch,
                pg_catalog.encode(public.gen_random_bytes(32),'hex'),
                NULL,v_state.backend_incarnation,v_endpoint,
                aap_inference_routing_v1._routing_base64url(
                    public.gen_random_bytes(32)
                ),
                pg_catalog.encode(public.digest(pg_catalog.convert_to(
                    aap_inference_routing_v1._video_execution_canonical_json(v_transport),
                    'UTF8'
                ),'sha256'),'hex'),
                v_registry_snapshot,v_transport
            );
        END LOOP;
        v_job_helper := aap_jobs_v1.video_restore_authority(
            'prepare',pg_catalog.jsonb_build_object(
                'restore_id',v_restore_id,
                'source_jobs_realm',v_source_jobs_realm,
                'target_jobs_realm',v_target_jobs_realm,
                'source_jobs_secret_sha256',v_source_jobs_secret_sha
            )
        );
        IF v_job_helper IS NULL OR v_job_helper -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR pg_catalog.jsonb_typeof(v_job_helper -> 'routes') IS DISTINCT FROM 'array' THEN
            v_reason := COALESCE(v_job_helper ->> 'reason','route_snapshot_invalid');
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='video restore rejected';
        END IF;
        v_route_snapshot := v_job_helper -> 'routes';
        FOR v_route IN
            SELECT item.value FROM pg_catalog.jsonb_array_elements(v_route_snapshot) AS item(value)
        LOOP
            BEGIN
                v_request_id := aap_inference_routing_v1._video_restore_uuid(
                    v_route -> 'request_id'
                );
                v_scope_id := aap_inference_routing_v1._video_restore_uuid(
                    v_route -> 'scope_id'
                );
                v_registry_id := CASE
                    WHEN v_route -> 'registry_id' = 'null'::jsonb THEN NULL
                    ELSE aap_inference_routing_v1._video_restore_uuid(
                        v_route -> 'registry_id'
                    )
                END;
            EXCEPTION WHEN OTHERS THEN
                v_reason := 'route_snapshot_invalid';
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='video restore rejected';
            END;
            v_digest := v_route ->> 'command_sha256';
            IF NOT aap_inference_routing_v1._video_restore_digest_valid(v_digest) THEN
                v_reason := 'route_snapshot_invalid';
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='video restore rejected';
            END IF;
            IF v_scope_id IS DISTINCT FROM
                aap_inference_routing_v1._video_execution_scope_uuid(v_request_id) THEN
                v_reason := 'route_snapshot_invalid';
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='video restore rejected';
            END IF;
            IF v_route ->> 'state'='bound' THEN
                SELECT scope.* INTO v_scope
                FROM aap_inference_routing_v1.scopes AS scope
                WHERE scope.id=v_scope_id;
                IF NOT FOUND
                   OR v_scope.command_sha256 IS DISTINCT FROM v_digest
                   OR v_scope.owner IS DISTINCT FROM
                        'tracker:' || (v_route ->> 'job_id')
                   OR v_scope.operation IS DISTINCT FROM 'tracker'
                   OR (v_registry_id IS NOT NULL
                       AND v_scope.registry_id IS DISTINCT FROM v_registry_id)
                   OR NOT EXISTS (
                        SELECT 1
                        FROM aap_inference_routing_v1.video_restore_scopes AS snapshot
                        WHERE snapshot.restore_id=v_restore_id
                          AND snapshot.scope_id=v_scope_id
                          AND snapshot.registry_id=v_scope.registry_id
                   ) THEN
                    v_reason := 'route_snapshot_invalid';
                    RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='video restore rejected';
                END IF;
            ELSIF v_route ->> 'state'='prepared' THEN
                SELECT receipt.realm,receipt.result,receipt.command_sha256,
                       receipt.operation
                INTO v_receipt
                FROM aap_inference_routing_v1.command_receipts AS receipt
                WHERE receipt.id=v_request_id;
                IF NOT FOUND
                   OR v_receipt.command_sha256 IS DISTINCT FROM v_digest
                   OR v_receipt.operation IS DISTINCT FROM 'video_acquire'
                   OR NOT aap_inference_routing_v1._routing_exact_keys(
                        v_receipt.result,ARRAY['reason']
                   )
                   OR pg_catalog.jsonb_typeof(v_receipt.result -> 'reason')
                        IS DISTINCT FROM 'string'
                   OR EXISTS (
                        SELECT 1 FROM aap_inference_routing_v1.scopes AS scope
                        WHERE scope.id=v_scope_id
                   )
                   OR EXISTS (
                        SELECT 1 FROM aap_inference_routing_v1.video_execution_scope_permits
                        WHERE scope_id=v_scope_id
                   )
                   OR NOT (
                        v_receipt.realm=v_source_routing_realm
                        OR aap_inference_routing_v1._video_execution_no_scope_rejection(
                            v_scope_id,v_request_id,v_digest
                        )
                   ) THEN
                    v_reason := 'route_snapshot_invalid';
                    RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='video restore rejected';
                END IF;
                INSERT INTO aap_inference_routing_v1.video_restore_no_scope_receipts(
                    restore_id,request_id,scope_id,command_sha256,receipt_realm,
                    source_routing_realm,target_routing_realm
                ) VALUES (
                    v_restore_id,v_request_id,v_scope_id,v_digest,v_receipt.realm,
                    v_source_routing_realm,v_target_routing_realm
                );
            ELSE
                v_reason := 'route_snapshot_invalid';
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='video restore rejected';
            END IF;
        END LOOP;
        v_context_output := aap_inference_routing_v1._video_restore_contexts(v_restore_id);
        SELECT COALESCE(pg_catalog.jsonb_agg(
            item.value || pg_catalog.jsonb_build_object(
                'retired_epoch','00000000-0000-0000-0000-000000000000'
            ) ORDER BY item.ordinality
        ),'[]'::jsonb)
        INTO v_context_output
        FROM pg_catalog.jsonb_array_elements(v_context_output)
             WITH ORDINALITY AS item(value,ordinality);
        v_output := aap_inference_routing_v1._video_restore_reply(
            pg_catalog.jsonb_build_object(
                'restore',aap_inference_routing_v1._video_restore_record(v_restore_id),
                'contexts',v_context_output
            )
        );
        IF pg_catalog.octet_length(v_output::text)>65536 THEN
            v_reason := 'registry_unavailable';
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='video restore rejected';
        END IF;
        RETURN aap_inference_routing_v1._video_restore_reply(
            pg_catalog.jsonb_build_object(
                'restore',aap_inference_routing_v1._video_restore_record(v_restore_id),
                'contexts',aap_inference_routing_v1._video_restore_contexts(v_restore_id)
            )
        );
    END IF;
    IF p_action='status' THEN
        BEGIN
            v_restore_id := aap_inference_routing_v1._video_restore_uuid(
                v_args -> 'restore_id'
            );
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
        END;
        SELECT record.* INTO v_record
        FROM aap_inference_routing_v1.video_restore_records AS record
        WHERE record.restore_id=v_restore_id;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'restore_missing');
        END IF;
        v_output := aap_inference_routing_v1._video_restore_reply(
            pg_catalog.jsonb_build_object(
                'restore',aap_inference_routing_v1._video_restore_record(v_restore_id),
                'contexts',aap_inference_routing_v1._video_restore_contexts(v_restore_id)
            )
        );
        IF pg_catalog.octet_length(v_output::text)>65536 THEN
            v_reason := 'storage_unavailable';
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='video restore rejected';
        END IF;
        RETURN v_output;
    END IF;

    IF p_action IN ('observe','record_bound') THEN
        BEGIN
            v_restore_id := aap_inference_routing_v1._video_restore_uuid(
                v_args -> 'restore_id'
            );
            v_registry_id := aap_inference_routing_v1._video_restore_uuid(
                v_args -> 'registry_id'
            );
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
        END;
        v_reply := v_args -> 'reply';
        IF NOT aap_inference_routing_v1._video_restore_exact_keys(
            v_reply,ARRAY[
                'schema','command_id','registry_id','authority_epoch',
                'backend_incarnation','challenge','phase','active_requests'
            ]
        ) OR v_reply ->> 'schema' IS DISTINCT FROM
                'aap-backend-route-control-reply/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'command_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'registry_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'authority_epoch') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'backend_incarnation') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'challenge') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'phase') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_reply -> 'active_requests') IS DISTINCT FROM 'number'
           OR (v_reply ->> 'active_requests') !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.char_length(v_reply ->> 'active_requests')>19
           OR v_reply ->> 'challenge' !~ '^[0-9a-f]{64}$'
           OR v_reply ->> 'phase' NOT IN ('draining','drained','bound') THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                NULL,'backend_reply_invalid'
            );
        END IF;
        BEGIN
            PERFORM aap_inference_routing_v1._video_restore_uuid(
                v_reply -> 'command_id'
            );
            PERFORM aap_inference_routing_v1._video_restore_uuid(
                v_reply -> 'registry_id'
            );
            PERFORM aap_inference_routing_v1._video_restore_uuid(
                v_reply -> 'authority_epoch'
            );
            PERFORM aap_inference_routing_v1._video_restore_uuid(
                v_reply -> 'backend_incarnation'
            );
            v_active_requests := (v_reply ->> 'active_requests')::bigint;
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                NULL,'backend_reply_invalid'
            );
        END;
        v_phase := v_reply ->> 'phase';
        IF (v_phase='draining' AND v_active_requests=0)
           OR (v_phase='drained' AND v_active_requests<>0) THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                NULL,'backend_reply_invalid'
            );
        END IF;
        SELECT record.* INTO v_record
        FROM aap_inference_routing_v1.video_restore_records AS record
        WHERE record.restore_id=v_restore_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'restore_missing');
        END IF;
        IF v_record.phase='completed' THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                pg_catalog.jsonb_build_object(
                    'restore',aap_inference_routing_v1._video_restore_record(v_restore_id),
                    'contexts','[]'::jsonb
                )
            );
        END IF;
        SELECT context.* INTO v_context
        FROM aap_inference_routing_v1.video_restore_contexts AS context
        WHERE context.restore_id=v_restore_id
          AND context.registry_id=v_registry_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                NULL,'registry_unavailable'
            );
        END IF;
        IF v_reply ->> 'registry_id' IS DISTINCT FROM v_context.registry_id::text
           OR v_reply ->> 'backend_incarnation'
                IS DISTINCT FROM v_context.backend_incarnation::text THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                NULL,'backend_reply_invalid'
            );
        END IF;
        IF p_action='observe' THEN
            IF v_context.retired_epoch IS NULL THEN
                IF v_phase IS DISTINCT FROM 'bound'
                   OR v_reply ->> 'authority_epoch' IS NOT DISTINCT FROM
                        v_context.new_epoch::text THEN
                    RETURN aap_inference_routing_v1._video_restore_reply(
                        NULL,'backend_reply_invalid'
                    );
                END IF;
                UPDATE aap_inference_routing_v1.video_restore_contexts AS context
                SET retired_epoch=(v_reply ->> 'authority_epoch')::uuid,
                    observed_reply=v_reply
                WHERE context.restore_id=v_restore_id
                  AND context.registry_id=v_registry_id;
            ELSIF v_reply IS DISTINCT FROM v_context.observed_reply
               AND NOT (
                    v_reply ->> 'command_id'=v_context.control_id::text
                    AND v_reply ->> 'authority_epoch'=v_context.new_epoch::text
                    AND v_reply ->> 'challenge'=v_context.challenge
               ) THEN
                RETURN aap_inference_routing_v1._video_restore_reply(
                    NULL,'backend_reply_invalid'
                );
            END IF;
        ELSE
            IF v_context.retired_epoch IS NULL
               OR v_phase IS DISTINCT FROM 'bound'
               OR v_active_requests<>0
               OR v_reply ->> 'command_id' IS DISTINCT FROM v_context.control_id::text
               OR v_reply ->> 'authority_epoch' IS DISTINCT FROM v_context.new_epoch::text
               OR v_reply ->> 'challenge' IS DISTINCT FROM v_context.challenge THEN
                RETURN aap_inference_routing_v1._video_restore_reply(
                    NULL,'backend_reply_invalid'
                );
            END IF;
            IF v_context.phase='bound' THEN
                IF v_context.bound_reply IS DISTINCT FROM v_reply THEN
                    RETURN aap_inference_routing_v1._video_restore_reply(
                        NULL,'backend_reply_invalid'
                    );
                END IF;
            ELSE
                UPDATE aap_inference_routing_v1.video_restore_contexts AS context
                SET phase='bound',bound_reply=v_reply
                WHERE context.restore_id=v_restore_id
                  AND context.registry_id=v_registry_id;
            END IF;
        END IF;
        v_output := aap_inference_routing_v1._video_restore_reply(
            pg_catalog.jsonb_build_object(
                'restore',aap_inference_routing_v1._video_restore_record(v_restore_id),
                'contexts',aap_inference_routing_v1._video_restore_contexts(v_restore_id)
            )
        );
        IF pg_catalog.octet_length(v_output::text)>65536 THEN
            v_reason := 'storage_unavailable';
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='video restore rejected';
        END IF;
        RETURN v_output;
    END IF;
    IF p_action='finish' THEN
        BEGIN
            v_restore_id := aap_inference_routing_v1._video_restore_uuid(
                v_args -> 'restore_id'
            );
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
        END;
        SELECT record.* INTO v_record
        FROM aap_inference_routing_v1.video_restore_records AS record
        WHERE record.restore_id=v_restore_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'restore_missing');
        END IF;
        IF v_record.phase='completed' THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                pg_catalog.jsonb_build_object(
                    'restore',aap_inference_routing_v1._video_restore_record(v_restore_id),
                    'contexts','[]'::jsonb
                )
            );
        END IF;
        SELECT authority.* INTO v_route_authority
        FROM aap_inference_routing_v1.authority_secret AS authority
        WHERE authority.singleton IS TRUE
        FOR UPDATE NOWAIT;
        SELECT database.oid AS database_oid,
               pg_catalog.current_database() AS database_name
        INTO v_current_identity
        FROM pg_catalog.pg_database AS database
        WHERE database.datname=pg_catalog.current_database();
        IF NOT FOUND OR v_route_authority.singleton IS DISTINCT FROM true
           OR pg_catalog.octet_length(v_route_authority.secret)<>32
           OR v_route_authority.realm_database_oid::text || ':' ||
                v_route_authority.realm_database_name
                IS DISTINCT FROM v_record.source_routing_realm
           OR pg_catalog.encode(public.digest(v_route_authority.secret,'sha256'),'hex')
                IS DISTINCT FROM v_record.source_routing_secret_sha256
           OR v_current_identity.database_oid::text || ':' ||
                v_current_identity.database_name
                IS DISTINCT FROM v_record.target_routing_realm
           OR v_current_identity.database_oid::text || ':' ||
                v_current_identity.database_name
                IS DISTINCT FROM
                (v_record.command ->> 'target_routing_realm') THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                NULL,'source_mismatch'
            );
        END IF;
        v_target_routing_oid := v_current_identity.database_oid;
        v_target_routing_name := v_current_identity.database_name;
        IF EXISTS (
            SELECT 1
            FROM aap_inference_routing_v1.video_restore_contexts AS context
            WHERE context.restore_id=v_restore_id
              AND (
                  context.phase IS DISTINCT FROM 'bound'
                  OR context.retired_epoch IS NULL
                  OR context.bound_reply IS NULL
                  OR context.bound_reply ->> 'schema'
                        IS DISTINCT FROM 'aap-backend-route-control-reply/v1'
                  OR context.bound_reply ->> 'command_id'
                        IS DISTINCT FROM context.control_id::text
                  OR context.bound_reply ->> 'registry_id'
                        IS DISTINCT FROM context.registry_id::text
                  OR context.bound_reply ->> 'authority_epoch'
                        IS DISTINCT FROM context.new_epoch::text
                  OR context.bound_reply ->> 'backend_incarnation'
                        IS DISTINCT FROM context.backend_incarnation::text
                  OR context.bound_reply ->> 'challenge'
                        IS DISTINCT FROM context.challenge
                  OR context.bound_reply ->> 'phase' IS DISTINCT FROM 'bound'
                  OR context.bound_reply ->> 'active_requests' IS DISTINCT FROM '0'
              )
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_record.registry_state_snapshot) AS item(value)
            WHERE item.value ->> 'continuity'='ready'
              AND NOT EXISTS (
                  SELECT 1
                  FROM aap_inference_routing_v1.video_restore_contexts AS context
                  WHERE context.restore_id=v_restore_id
                    AND context.registry_id=(item.value ->> 'registry_id')::uuid
                    AND context.phase='bound'
              )
        ) THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                NULL,'retirement_incomplete'
            );
        END IF;
        IF EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.controls AS control
            WHERE control.phase IN ('prepared','draining','uncertain')
        ) OR EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.control_attempts AS attempt
            WHERE attempt.state IN ('active','uncertain')
        ) OR EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.scope_retirements AS retirement
            WHERE retirement.phase IN ('prepared','draining')
        ) THEN
            RETURN aap_inference_routing_v1._video_restore_reply(NULL,'restore_busy');
        END IF;

        FOR v_lock IN
            SELECT state.registry_id
            FROM aap_inference_routing_v1.registry_state AS state
            ORDER BY state.registry_id
            FOR UPDATE NOWAIT
        LOOP
            NULL;
        END LOOP;
        FOR v_lock IN
            SELECT registry.id
            FROM public.ml_backend_registry AS registry
            ORDER BY registry.id
            FOR SHARE NOWAIT
        LOOP
            NULL;
        END LOOP;
        FOR v_lock IN
            SELECT scope.scope_id
            FROM aap_inference_routing_v1.video_restore_scopes AS scope
            WHERE scope.restore_id=v_restore_id
            ORDER BY scope.scope_id
        LOOP
            SELECT current_scope.* INTO v_scope
            FROM aap_inference_routing_v1.scopes AS current_scope
            WHERE current_scope.id=v_lock.scope_id
            FOR UPDATE NOWAIT;
            IF NOT FOUND OR pg_catalog.to_jsonb(v_scope) IS DISTINCT FROM (
                SELECT snapshot.scope_snapshot
                FROM aap_inference_routing_v1.video_restore_scopes AS snapshot
                WHERE snapshot.restore_id=v_restore_id
                  AND snapshot.scope_id=v_lock.scope_id
            ) THEN
                RETURN aap_inference_routing_v1._video_restore_reply(
                    NULL,'transport_changed'
                );
            END IF;
        END LOOP;
        FOR v_lock IN
            SELECT attempt.attempt_id
            FROM aap_inference_routing_v1.video_restore_attempts AS attempt
            WHERE attempt.restore_id=v_restore_id
            ORDER BY attempt.attempt_id
        LOOP
            PERFORM 1
            FROM aap_inference_routing_v1.attempts AS current_attempt
            WHERE current_attempt.id=v_lock.attempt_id
            FOR UPDATE NOWAIT;
            IF NOT FOUND OR (
                SELECT pg_catalog.to_jsonb(current_attempt)
                FROM aap_inference_routing_v1.attempts AS current_attempt
                WHERE current_attempt.id=v_lock.attempt_id
            ) IS DISTINCT FROM (
                SELECT snapshot.attempt_snapshot
                FROM aap_inference_routing_v1.video_restore_attempts AS snapshot
                WHERE snapshot.restore_id=v_restore_id
                  AND snapshot.attempt_id=v_lock.attempt_id
            ) THEN
                RETURN aap_inference_routing_v1._video_restore_reply(
                    NULL,'transport_changed'
                );
            END IF;
        END LOOP;
        IF (SELECT pg_catalog.count(*)
            FROM aap_inference_routing_v1.scopes) IS DISTINCT FROM
           (SELECT pg_catalog.count(*)
            FROM aap_inference_routing_v1.video_restore_scopes AS snapshot
            WHERE snapshot.restore_id=v_restore_id)
           OR (SELECT pg_catalog.count(*)
               FROM aap_inference_routing_v1.attempts AS current_attempt
               JOIN aap_inference_routing_v1.video_restore_scopes AS scope
                 ON scope.restore_id=v_restore_id
                AND scope.scope_id=current_attempt.scope_id) IS DISTINCT FROM
              (SELECT pg_catalog.count(*)
               FROM aap_inference_routing_v1.video_restore_attempts AS snapshot
               WHERE snapshot.restore_id=v_restore_id)
           OR (SELECT pg_catalog.count(*) FROM aap_inference_routing_v1.registry_state)
                IS DISTINCT FROM
              pg_catalog.jsonb_array_length(v_record.registry_state_snapshot) THEN
            RETURN aap_inference_routing_v1._video_restore_reply(
                NULL,'transport_changed'
            );
        END IF;
        FOR v_route IN
            SELECT item.value
            FROM pg_catalog.jsonb_array_elements(v_record.registry_state_snapshot) AS item(value)
            ORDER BY item.value ->> 'registry_id'
        LOOP
            v_registry_id := (v_route ->> 'registry_id')::uuid;
            v_current_registry_snapshot :=
                aap_inference_routing_v1._video_restore_registry_snapshot(v_registry_id);
            IF v_current_registry_snapshot IS DISTINCT FROM v_route THEN
                RETURN aap_inference_routing_v1._video_restore_reply(
                    NULL,'transport_changed'
                );
            END IF;
        END LOOP;
        FOR v_context IN
            SELECT context.*
            FROM aap_inference_routing_v1.video_restore_contexts AS context
            WHERE context.restore_id=v_restore_id
            ORDER BY context.registry_id
            FOR UPDATE NOWAIT
        LOOP
            v_current_transport :=
                aap_inference_routing_v1._routing_transport_identity(v_context.registry_id);
            v_current_registry_snapshot :=
                aap_inference_routing_v1._video_restore_registry_snapshot(
                    v_context.registry_id
                );
            IF v_current_transport IS DISTINCT FROM v_context.transport_snapshot
               OR v_current_registry_snapshot IS DISTINCT FROM
                    v_context.registry_snapshot
               OR pg_catalog.encode(public.digest(pg_catalog.convert_to(
                    aap_inference_routing_v1._video_execution_canonical_json(
                        v_current_transport
                    ),'UTF8'
                  ),'sha256'),'hex') IS DISTINCT FROM
                    v_context.transport_identity_sha256 THEN
                RETURN aap_inference_routing_v1._video_restore_reply(
                    NULL,'transport_changed'
                );
            END IF;
        END LOOP;

        v_job_helper := aap_jobs_v1.video_restore_authority(
            'finish',pg_catalog.jsonb_build_object(
                'restore_id',v_restore_id,
                'source_jobs_realm',v_record.source_jobs_realm,
                'target_jobs_realm',v_record.target_jobs_realm,
                'source_jobs_secret_sha256',v_record.source_jobs_secret_sha256
            )
        );
        IF v_job_helper IS NULL OR v_job_helper -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR pg_catalog.jsonb_typeof(v_job_helper -> 'executions_invalidated')
                IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(v_job_helper -> 'route_intents_rewrapped')
                IS DISTINCT FROM 'number' THEN
            v_reason := COALESCE(v_job_helper ->> 'reason','storage_unavailable');
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='video restore rejected';
        END IF;
        v_executions_invalidated :=
            (v_job_helper ->> 'executions_invalidated')::bigint;
        v_routes_rewrapped := (v_job_helper ->> 'route_intents_rewrapped')::bigint;
        v_registries_rebound := (
            SELECT pg_catalog.count(*)
            FROM aap_inference_routing_v1.video_restore_contexts AS context
            WHERE context.restore_id=v_restore_id
        );
        v_scopes_retired := (
            SELECT pg_catalog.count(*)
            FROM aap_inference_routing_v1.video_restore_scopes AS scope
            WHERE scope.restore_id=v_restore_id
        );
        INSERT INTO aap_inference_routing_v1.video_restore_scope_retirements(
            scope_id,restore_id,registry_id,source_routing_realm,
            target_routing_realm,scope_realm,original_epoch,retired_epoch,
            new_epoch,backend_incarnation,control_id,challenge,
            transport_identity_sha256
        )
        SELECT snapshot.scope_id,v_restore_id,snapshot.registry_id,
               v_record.source_routing_realm,v_record.target_routing_realm,
               snapshot.scope_realm,snapshot.original_epoch,context.retired_epoch,
               context.new_epoch,context.backend_incarnation,context.control_id,
               context.challenge,context.transport_identity_sha256
        FROM aap_inference_routing_v1.video_restore_scopes AS snapshot
        JOIN aap_inference_routing_v1.video_restore_contexts AS context
          ON context.restore_id=snapshot.restore_id
         AND context.registry_id=snapshot.registry_id
        WHERE snapshot.restore_id=v_restore_id
        ORDER BY snapshot.scope_id;
        UPDATE aap_inference_routing_v1.attempts AS attempt
        SET state='reconciled',updated_at=v_now
        WHERE attempt.state IN ('active','uncertain')
          AND EXISTS (
              SELECT 1 FROM aap_inference_routing_v1.video_restore_scopes AS snapshot
              WHERE snapshot.restore_id=v_restore_id
                AND snapshot.scope_id=attempt.scope_id
          );
        GET DIAGNOSTICS v_attempts_reconciled=ROW_COUNT;
        UPDATE aap_inference_routing_v1.scopes AS scope
        SET state='reconciled',closed=true,reconciled_by=v_restore_id,
            updated_at=v_now
        WHERE scope.state IS DISTINCT FROM 'reconciled'
          AND EXISTS (
              SELECT 1 FROM aap_inference_routing_v1.video_restore_scopes AS snapshot
              WHERE snapshot.restore_id=v_restore_id
                AND snapshot.scope_id=scope.id
          );
        FOR v_context IN
            SELECT context.*
            FROM aap_inference_routing_v1.video_restore_contexts AS context
            WHERE context.restore_id=v_restore_id
            ORDER BY context.registry_id
        LOOP
            UPDATE aap_inference_routing_v1.registry_state AS state
            SET authority_epoch=v_context.new_epoch,
                backend_incarnation=v_context.backend_incarnation,
                continuity='ready',
                epoch_key=aap_inference_routing_v1._routing_unbase64url(
                    v_context.signing_key
                ),
                realm=v_record.target_routing_realm,
                updated_at=v_now
            WHERE state.registry_id=v_context.registry_id;
        END LOOP;
        UPDATE aap_inference_routing_v1.registry_state AS state
        SET realm=v_record.target_routing_realm,updated_at=v_now
        WHERE NOT EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.video_restore_contexts AS context
            WHERE context.restore_id=v_restore_id
              AND context.registry_id=state.registry_id
        );
        UPDATE aap_inference_routing_v1.authority_secret AS authority
        SET secret=public.gen_random_bytes(32),
            realm_database_oid=v_target_routing_oid,
            realm_database_name=v_target_routing_name
        WHERE authority.singleton IS TRUE;
        v_counts := pg_catalog.jsonb_build_object(
            'executions_invalidated',v_executions_invalidated,
            'route_intents_rewrapped',v_routes_rewrapped,
            'registries_rebound',v_registries_rebound,
            'scopes_retired',v_scopes_retired,
            'attempts_reconciled',v_attempts_reconciled
        );
        UPDATE aap_inference_routing_v1.video_restore_contexts AS context
        SET endpoint='{}'::jsonb,signing_key='',
            registry_snapshot='{}'::jsonb,transport_snapshot='{}'::jsonb
        WHERE context.restore_id=v_restore_id;
        UPDATE aap_inference_routing_v1.video_restore_records AS record
        SET phase='completed',completed_at=v_now,counts=v_counts
        WHERE record.restore_id=v_restore_id AND record.phase='prepared';
        v_output := aap_inference_routing_v1._video_restore_reply(
            pg_catalog.jsonb_build_object(
                'restore',aap_inference_routing_v1._video_restore_record(v_restore_id),
                'contexts','[]'::jsonb
            )
        );
        IF pg_catalog.octet_length(v_output::text)>65536 THEN
            v_reason := 'storage_unavailable';
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='video restore rejected';
        END IF;
        RETURN v_output;
    END IF;
    RETURN aap_inference_routing_v1._video_restore_reply(NULL,'invalid_command');
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_inference_routing_v1._video_restore_reply(NULL,'execution_busy');
    WHEN SQLSTATE 'P0001' THEN
        RETURN aap_inference_routing_v1._video_restore_reply(
            NULL,COALESCE(v_reason,'storage_unavailable')
        );
    WHEN OTHERS THEN
        RETURN aap_inference_routing_v1._video_restore_reply(NULL,'storage_unavailable');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_routing_v1.video_restore_step(text,text) FROM PUBLIC;
