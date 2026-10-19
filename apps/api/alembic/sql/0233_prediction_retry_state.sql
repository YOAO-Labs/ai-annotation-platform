-- Durable failed-prediction retry history is Labeling-owned. Delivery and the
-- stable async Job identity are Jobs-owned. Neither table follows live-row FKs.
CREATE TABLE public.prediction_retry_attempts (
    job_id uuid PRIMARY KEY,
    failed_prediction_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id uuid,
    ml_backend_id uuid,
    ml_backend_pool_id uuid,
    source_extra jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(source_extra) = 'object'),
    retry_count_at_admission integer NOT NULL CHECK (retry_count_at_admission >= 0),
    write_scope jsonb CHECK (write_scope IS NULL OR pg_catalog.jsonb_typeof(write_scope) = 'object'),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    execution_queue text CHECK (execution_queue IS NULL OR pg_catalog.octet_length(execution_queue) BETWEEN 1 AND 256),
    legacy_delivery boolean NOT NULL DEFAULT false,
    state text NOT NULL CHECK (state IN ('accepted','applying','failing','succeeded','failed','abandoned')),
    outcome jsonb CHECK (outcome IS NULL OR pg_catalog.jsonb_typeof(outcome) = 'object'),
    terminal_digest text CHECK (terminal_digest IS NULL OR terminal_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK ((state IN ('accepted','applying','failing') AND outcome IS NULL AND terminal_digest IS NULL)
        OR (state IN ('succeeded','failed','abandoned') AND outcome IS NOT NULL AND terminal_digest IS NOT NULL)),
    CHECK (pg_catalog.octet_length(source_extra::text) <= 2000000),
    CHECK (outcome IS NULL OR pg_catalog.octet_length(outcome::text) <= 2000000)
);
-- aap-statement-break
CREATE UNIQUE INDEX uq_prediction_retry_attempts_one_active_failure
    ON public.prediction_retry_attempts(failed_prediction_id)
    WHERE state IN ('accepted','applying','failing');
-- aap-statement-break
CREATE INDEX ix_prediction_retry_attempts_actor_created
    ON public.prediction_retry_attempts(actor_id, created_at DESC);
-- aap-statement-break
CREATE TABLE public.prediction_retry_deliveries (
    job_id uuid PRIMARY KEY,
    failed_prediction_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    project_id uuid NOT NULL,
    execution_queue text CHECK (execution_queue IS NULL OR pg_catalog.octet_length(execution_queue) BETWEEN 1 AND 256),
    state text NOT NULL CHECK (state IN ('pending','claimed','submitted','terminal')),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    worker_id uuid,
    lease_token uuid,
    lease_expires_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    submitted_at timestamptz,
    last_error_code text CHECK (last_error_code IS NULL OR pg_catalog.octet_length(last_error_code) BETWEEN 1 AND 256),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK ((state = 'claimed' AND worker_id IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (state <> 'claimed' AND worker_id IS NULL AND lease_expires_at IS NULL)),
    CHECK (generation <= 9223372036854775807)
);
-- aap-statement-break
CREATE INDEX ix_prediction_retry_deliveries_due
    ON public.prediction_retry_deliveries(state, next_attempt_at, lease_expires_at, created_at);
-- aap-statement-break
REVOKE ALL ON public.prediction_retry_attempts, public.prediction_retry_deliveries FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._prediction_retry_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_allowed text[] := ARRAY[
        'invalid_request','invalid_response','not_found','permission_denied',
        'account_not_active','already_pending','retry_limit','dismissed',
        'scope_mismatch','execution_busy','lease_lost','job_not_writable',
        'result_conflict','result_not_ready','storage_unavailable',
        'content_deleting','content_epoch_changed'
    ];
    v_reply jsonb;
BEGIN
    IF p_code IS NULL THEN
        v_reply := pg_catalog.jsonb_build_object(
            'schema','aap-prediction-retries/v1','ok',true,'data',p_data
        );
        IF pg_catalog.octet_length(v_reply::text)>2097152 THEN
            RETURN pg_catalog.jsonb_build_object('schema','aap-prediction-retries/v1','ok',false,
                'error',pg_catalog.jsonb_build_object('code','invalid_response'));
        END IF;
        RETURN v_reply;
    END IF;
    IF p_code = ANY(v_allowed) THEN
        v_reply := pg_catalog.jsonb_build_object(
            'schema','aap-prediction-retries/v1','ok',false,
            'error',pg_catalog.jsonb_build_object('code',p_code)
        );
        RETURN v_reply;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema','aap-prediction-retries/v1','ok',false,
        'error',pg_catalog.jsonb_build_object('code','invalid_response')
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._prediction_retry_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._prediction_retry_error_code(p_code text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT CASE p_code
        WHEN 'job_missing' THEN 'not_found'
        WHEN 'selection_conflict' THEN 'scope_mismatch'
        WHEN 'result_incomplete' THEN 'result_not_ready'
        WHEN 'result_not_ready' THEN 'result_not_ready'
        WHEN 'result_conflict' THEN 'result_conflict'
        WHEN 'scope_mismatch' THEN 'scope_mismatch'
        WHEN 'execution_busy' THEN 'execution_busy'
        WHEN 'lease_lost' THEN 'lease_lost'
        WHEN 'job_not_writable' THEN 'job_not_writable'
        WHEN 'invalid_request' THEN 'invalid_request'
        WHEN 'invalid_response' THEN 'invalid_response'
        WHEN 'storage_unavailable' THEN 'storage_unavailable'
        WHEN 'content_deleting' THEN 'content_deleting'
        WHEN 'content_epoch_changed' THEN 'content_epoch_changed'
        ELSE 'invalid_response'
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._prediction_retry_error_code(text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._prediction_retry_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_reply jsonb;
BEGIN
    IF p_code IS NULL THEN
        v_reply := pg_catalog.jsonb_build_object(
            'schema','aap-prediction-retries/v1','ok',true,'data',p_data
        );
        IF pg_catalog.octet_length(v_reply::text)>2097152 THEN
            RETURN pg_catalog.jsonb_build_object('schema','aap-prediction-retries/v1','ok',false,
                'error',pg_catalog.jsonb_build_object('code','invalid_response'));
        END IF;
        RETURN v_reply;
    END IF;
    IF p_code = ANY(ARRAY[
        'invalid_request','invalid_response','not_found','permission_denied',
        'account_not_active','already_pending','retry_limit','dismissed',
        'scope_mismatch','execution_busy','lease_lost','job_not_writable',
        'result_conflict','result_not_ready','storage_unavailable',
        'content_deleting','content_epoch_changed'
    ]) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-prediction-retries/v1','ok',false,
            'error',pg_catalog.jsonb_build_object('code',p_code)
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema','aap-prediction-retries/v1','ok',false,
        'error',pg_catalog.jsonb_build_object('code','invalid_response')
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._prediction_retry_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._prediction_retry_attempt_json(
    p_attempt public.prediction_retry_attempts
)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'job_id',p_attempt.job_id,
        'failed_prediction_id',p_attempt.failed_prediction_id,
        'actor_id',p_attempt.actor_id,
        'project_id',p_attempt.project_id,
        'task_id',p_attempt.task_id,
        'ml_backend_id',p_attempt.ml_backend_id,
        'ml_backend_pool_id',p_attempt.ml_backend_pool_id,
        'source_extra',p_attempt.source_extra,
        'retry_count_at_admission',p_attempt.retry_count_at_admission,
        'write_scope',p_attempt.write_scope,
        'request_digest',p_attempt.request_digest,
        'execution_queue',p_attempt.execution_queue,
        'state',p_attempt.state,
        'outcome',p_attempt.outcome
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._prediction_retry_attempt_json(public.prediction_retry_attempts) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._prediction_retry_delivery_json(
    p_delivery public.prediction_retry_deliveries
)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'job_id',p_delivery.job_id,
        'failed_prediction_id',p_delivery.failed_prediction_id,
        'actor_id',p_delivery.actor_id,
        'execution_queue',p_delivery.execution_queue,
        'generation',p_delivery.generation,
        'lease_token',p_delivery.lease_token,
        'lease_expires_at',p_delivery.lease_expires_at
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._prediction_retry_delivery_json(public.prediction_retry_deliveries) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._prediction_retry_no_raw_data(p_value jsonb)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT p_value IS NOT NULL
       AND NOT pg_catalog.jsonb_path_exists(p_value, '$.**.raw_result')
       AND NOT pg_catalog.jsonb_path_exists(p_value, '$.**.results')
       AND NOT pg_catalog.jsonb_path_exists(p_value, '$.**.predictions')
       AND NOT pg_catalog.jsonb_path_exists(p_value, '$.**.write_requests')
       AND NOT pg_catalog.jsonb_path_exists(p_value, '$.**.output')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._prediction_retry_no_raw_data(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._prediction_retry_attempt_scope_valid(
    p_scope jsonb,
    p_allow_terminal boolean DEFAULT false
)
RETURNS boolean
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_attempt public.prediction_retry_attempts%ROWTYPE;
BEGIN
    IF NOT aap_jobs_v1._batch_result_scope_valid(p_scope, p_scope ? 'generation') THEN
        RETURN false;
    END IF;
    SELECT * INTO v_attempt
    FROM public.prediction_retry_attempts
    WHERE job_id=(p_scope ->> 'job_id')::uuid;
    IF NOT FOUND
       OR v_attempt.actor_id IS DISTINCT FROM (p_scope ->> 'actor_id')::uuid
       OR v_attempt.project_id IS DISTINCT FROM (p_scope ->> 'project_id')::uuid
       OR v_attempt.request_digest IS DISTINCT FROM p_scope ->> 'request_digest' THEN
        RETURN false;
    END IF;
    IF v_attempt.state IN ('accepted','applying','failing') THEN
        RETURN true;
    END IF;
    RETURN COALESCE(p_allow_terminal,false)
       AND v_attempt.state IN ('succeeded','failed','abandoned');
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._prediction_retry_attempt_scope_valid(jsonb, boolean) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._prediction_retry_attempt_exists(p_job_id uuid)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.prediction_retry_attempts AS attempt
        WHERE attempt.job_id=p_job_id
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._prediction_retry_attempt_exists(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._prediction_retry_job_delete_allowed(p_job_id uuid)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.prediction_retry_attempts AS attempt
        WHERE attempt.job_id=p_job_id
          AND attempt.state IN ('succeeded','failed','abandoned')
          AND attempt.outcome IS NOT NULL
          AND attempt.terminal_digest IS NOT NULL
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._prediction_retry_job_delete_allowed(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._prediction_retry_member_scope_matches(
    p_job_id uuid,
    p_task_id uuid,
    p_scope jsonb
)
RETURNS boolean
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_attempt public.prediction_retry_attempts%ROWTYPE;
BEGIN
    SELECT * INTO v_attempt
    FROM public.prediction_retry_attempts
    WHERE job_id=p_job_id;
    RETURN FOUND
       AND v_attempt.task_id IS NOT DISTINCT FROM p_task_id
       AND v_attempt.write_scope IS NOT DISTINCT FROM p_scope
       AND p_scope ->> 'operation' IS NOT DISTINCT FROM 'prediction_retry'
       AND p_scope ->> 'job_id' IS NOT DISTINCT FROM p_job_id::text
       AND p_scope ->> 'task_id' IS NOT DISTINCT FROM p_task_id::text
       AND p_scope ->> 'project_id' IS NOT DISTINCT FROM v_attempt.project_id::text
       AND p_scope ->> 'actor_id' IS NOT DISTINCT FROM v_attempt.actor_id::text;
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._prediction_retry_member_scope_matches(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._prediction_retry_terminal_matches(
    p_job_id uuid,
    p_state text,
    p_outcome jsonb DEFAULT NULL
)
RETURNS boolean
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_attempt public.prediction_retry_attempts%ROWTYPE;
    v_failed public.failed_predictions%ROWTYPE;
    v_origin uuid;
BEGIN
    SELECT * INTO v_attempt FROM public.prediction_retry_attempts WHERE job_id=p_job_id;
    IF NOT FOUND OR v_attempt.state IS DISTINCT FROM p_state
       OR (p_outcome IS NOT NULL AND v_attempt.outcome IS DISTINCT FROM p_outcome) THEN
        RETURN false;
    END IF;
    v_origin := v_attempt.failed_prediction_id;
    IF p_state='succeeded' THEN
        RETURN v_attempt.outcome ->> 'status' = 'succeeded'
           AND v_attempt.outcome ->> 'failed_id' = v_origin::text
           AND v_attempt.outcome ->> 'prediction_id' = v_attempt.outcome #>> '{receipt,id}'
           AND v_attempt.outcome ->> 'result_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND v_attempt.outcome ->> 'prepared_digest' ~ '^[0-9a-f]{64}$'
           AND NOT EXISTS (SELECT 1 FROM public.failed_predictions WHERE id=v_origin);
    ELSIF p_state='failed' THEN
        SELECT * INTO v_failed FROM public.failed_predictions WHERE id=v_origin;
        RETURN FOUND
           AND v_attempt.outcome ->> 'status' = 'failed'
           AND v_attempt.outcome ->> 'failed_id' = v_origin::text
           AND (v_attempt.outcome ->> 'retry_count') ~ '^(0|[1-9][0-9]*)$'
           AND v_failed.retry_count=(v_attempt.outcome ->> 'retry_count')::integer
           AND v_failed.retry_count=v_attempt.retry_count_at_admission+1
           AND v_failed.extra ->> 'retry_job_id' IS DISTINCT FROM p_job_id::text
           AND v_failed.extra -> 'retry_pending' IS DISTINCT FROM 'true'::jsonb;
    ELSIF p_state='abandoned' THEN
        RETURN v_attempt.outcome ->> 'status' IN ('cancelled','failed')
           AND v_attempt.outcome ->> 'failed_id' = v_origin::text
           AND NOT EXISTS (
               SELECT 1 FROM public.failed_predictions AS failed
               WHERE failed.id=v_origin
                 AND failed.extra ->> 'retry_job_id'=p_job_id::text
                 AND failed.extra -> 'retry_pending'='true'::jsonb
           );
    END IF;
    RETURN false;
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._prediction_retry_terminal_matches(uuid, text, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._abandon_prediction_retry_from_job(
    p_job_id uuid,
    p_job_status text,
    p_reason text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_attempt public.prediction_retry_attempts%ROWTYPE;
    v_reason text := LEFT(COALESCE(p_reason,'job_terminalized'),200);
    v_status text;
    v_outcome jsonb;
BEGIN
    IF p_job_status NOT IN ('failed','cancelled') THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_request');
    END IF;
    SELECT * INTO v_attempt FROM public.prediction_retry_attempts
    WHERE job_id=p_job_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'not_found');
    END IF;
    IF v_attempt.state IN ('succeeded','failed','abandoned') THEN
        IF (p_job_status='completed' AND v_attempt.state='succeeded')
           OR (p_job_status='failed' AND v_attempt.state='failed')
           OR (p_job_status='cancelled' AND v_attempt.state='abandoned') THEN
            RETURN aap_labeling_v1._prediction_retry_reply(
                pg_catalog.jsonb_build_object('attempt',aap_labeling_v1._prediction_retry_attempt_json(v_attempt)),NULL
            );
        END IF;
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'job_not_writable');
    END IF;
    v_status := CASE WHEN p_job_status='cancelled' THEN 'cancelled' ELSE 'failed' END;
    v_outcome := pg_catalog.jsonb_build_object(
        'status',v_status,
        'failed_id',v_attempt.failed_prediction_id,
        'reason',v_reason,
        'error_code','job_terminalized'
    );
    UPDATE public.prediction_retry_attempts
    SET state='abandoned',outcome=v_outcome,
        terminal_digest=pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(v_outcome::text,'UTF8')),'hex'),
        updated_at=pg_catalog.clock_timestamp()
    WHERE job_id=p_job_id RETURNING * INTO v_attempt;
    UPDATE public.failed_predictions AS failed
    SET extra=(failed.extra - ARRAY['retry_pending','retry_job_id'])
    WHERE failed.id=v_attempt.failed_prediction_id
      AND failed.extra ->> 'retry_job_id'=p_job_id::text
      AND failed.extra -> 'retry_pending'='true'::jsonb;
    RETURN aap_labeling_v1._prediction_retry_reply(
        pg_catalog.jsonb_build_object('attempt',aap_labeling_v1._prediction_retry_attempt_json(v_attempt)),NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._abandon_prediction_retry_from_job(uuid, text, text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._retire_prediction_retry_results(p_job_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_count bigint;
BEGIN
    WITH retired AS (
        UPDATE public.batch_result_bundles AS bundle
        SET state='abandoned',updated_at=pg_catalog.clock_timestamp()
        WHERE bundle.job_id=p_job_id AND bundle.state IN ('capturing','captured','ready')
          AND bundle.receipt_count=0
        RETURNING bundle.result_id
    ), cleared AS (
        UPDATE public.batch_result_payloads AS payload
        SET body=NULL,roots_active=false,updated_at=pg_catalog.clock_timestamp()
        FROM retired WHERE payload.result_id=retired.result_id
        RETURNING payload.payload_id
    ), removed AS (
        DELETE FROM public.batch_result_parts AS part
        USING cleared WHERE part.payload_id=cleared.payload_id
        RETURNING part.payload_id
    )
    SELECT count(*)::bigint INTO v_count FROM retired;
    RETURN aap_labeling_v1._prediction_retry_reply(
        pg_catalog.jsonb_build_object('job_id',p_job_id,'retired_count',v_count),NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._retire_prediction_retry_results(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._create_prediction_retry_job(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_job uuid;
    v_failed uuid;
    v_actor uuid;
    v_project uuid;
    v_task uuid;
    v_digest text;
    v_queue text;
    v_scope jsonb;
    v_source_extra jsonb;
    v_attempt_scope jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','failed_prediction_id','actor_id','project_id','task_id','request_digest','execution_queue','write_scope']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','failed_prediction_id','actor_id','project_id','task_id','request_digest','execution_queue','write_scope'])
       OR pg_catalog.octet_length(p_payload::text)>2097152
       OR pg_catalog.jsonb_typeof(p_payload -> 'job_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'job_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'failed_prediction_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'failed_prediction_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'actor_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'actor_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'project_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'project_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'task_id') NOT IN ('string','null')
       OR (p_payload -> 'task_id'<>'null'::jsonb AND p_payload ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
       OR pg_catalog.jsonb_typeof(p_payload -> 'request_digest') IS DISTINCT FROM 'string'
       OR p_payload ->> 'request_digest' !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'execution_queue') NOT IN ('string','null')
       OR (p_payload -> 'execution_queue'<>'null'::jsonb AND pg_catalog.octet_length(p_payload ->> 'execution_queue') NOT BETWEEN 1 AND 256)
       OR pg_catalog.jsonb_typeof(p_payload -> 'write_scope') NOT IN ('object','null') THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'invalid_request');
    END IF;
    v_job := (p_payload ->> 'job_id')::uuid;
    v_failed := (p_payload ->> 'failed_prediction_id')::uuid;
    v_actor := (p_payload ->> 'actor_id')::uuid;
    v_project := (p_payload ->> 'project_id')::uuid;
    v_task := CASE WHEN p_payload -> 'task_id'='null'::jsonb THEN NULL ELSE (p_payload ->> 'task_id')::uuid END;
    v_digest := p_payload ->> 'request_digest';
    v_queue := CASE WHEN p_payload -> 'execution_queue'='null'::jsonb THEN NULL ELSE p_payload ->> 'execution_queue' END;
    v_scope := p_payload -> 'write_scope';
    IF (v_task IS NULL AND v_scope IS DISTINCT FROM 'null'::jsonb)
       OR (v_task IS NOT NULL AND (v_scope IS DISTINCT FROM pg_catalog.jsonb_build_object(
            'task_id',v_task,'project_id',v_project,'actor_id',v_actor,'actor_known',true,
            'operation','prediction_retry','execution_scope',NULL,'execution_scope_known',true,
            'task_version',v_scope -> 'task_version','task_status',v_scope -> 'task_status',
            'assignee_id',v_scope -> 'assignee_id','review_round_id',v_scope -> 'review_round_id',
            'source_annotation_id',NULL,'source_annotation_version',NULL,
            'expected_batch_id',v_scope -> 'expected_batch_id','expected_batch_id_known',true,
            'job_id',v_job,'job_id_known',true,'source_binding_known',true,
            'task_source_generation',v_scope -> 'task_source_generation',
            'source_dataset_item_id',v_scope -> 'source_dataset_item_id',
            'source_file_path',v_scope -> 'source_file_path','source_file_type',v_scope -> 'source_file_type',
            'accepted_input_id',v_scope -> 'accepted_input_id'
       )) OR v_scope ->> 'operation' IS DISTINCT FROM 'prediction_retry') THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'scope_mismatch');
    END IF;
    IF v_task IS NOT NULL AND NOT aap_labeling_v1._prediction_retry_member_scope_matches(v_job,v_task,v_scope) THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'scope_mismatch');
    END IF;
    IF NOT aap_labeling_v1._prediction_retry_attempt_scope_valid(
        pg_catalog.jsonb_build_object('job_id',v_job,'actor_id',v_actor,'project_id',v_project,'request_digest',v_digest),false
    ) THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'scope_mismatch');
    END IF;
    IF EXISTS (SELECT 1 FROM public.async_jobs WHERE id=v_job)
       OR EXISTS (SELECT 1 FROM public.batch_result_executions WHERE job_id=v_job)
       OR EXISTS (SELECT 1 FROM public.prediction_retry_deliveries WHERE job_id=v_job) THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'result_conflict');
    END IF;
    INSERT INTO public.async_jobs(
        id,kind,project_id,user_id,status,progress_pct,payload,result,celery_task_id,
        created_at,updated_at
    ) VALUES (
        v_job,'prediction_retry',v_project,v_actor,'pending',0,
        pg_catalog.jsonb_build_object(
            'failed_prediction_id',v_failed,'task_id',v_task,
            'prediction_retry_request_digest',v_digest,'execution_queue',v_queue
        ),'{}'::jsonb,v_job::text,pg_catalog.clock_timestamp(),pg_catalog.clock_timestamp()
    );
    INSERT INTO public.batch_result_executions(
        job_id,actor_id,project_id,request_digest,state,generation,total,
        selection_cursor,selection_complete,next_ordinal
    ) VALUES (
        v_job,v_actor,v_project,v_digest,'preparing',0,
        CASE WHEN v_task IS NULL THEN 0 ELSE 1 END,
        CASE WHEN v_task IS NULL THEN 0 ELSE 1 END,true,1
    );
    IF v_task IS NOT NULL THEN
        INSERT INTO public.batch_result_members(
            job_id,ordinal,task_id,write_scope,failure_id,state
        ) VALUES (v_job,1,v_task,v_scope,pg_catalog.gen_random_uuid(),'planned');
    END IF;
    INSERT INTO public.prediction_retry_deliveries(
        job_id,failed_prediction_id,actor_id,project_id,execution_queue,state,next_attempt_at
    ) VALUES (v_job,v_failed,v_actor,v_project,v_queue,'pending',pg_catalog.clock_timestamp());
    RETURN aap_jobs_v1._prediction_retry_reply(
        pg_catalog.jsonb_build_object('job_id',v_job,'created',true),NULL
    );
EXCEPTION WHEN unique_violation THEN
    RETURN aap_jobs_v1._prediction_retry_reply(NULL,'result_conflict');
WHEN OTHERS THEN
    RETURN aap_jobs_v1._prediction_retry_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._create_prediction_retry_job(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.admit_prediction_retry(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_job uuid;
    v_failed_id uuid;
    v_actor uuid;
    v_queue text;
    v_legacy boolean;
    v_existing public.prediction_retry_attempts%ROWTYPE;
    v_failed public.failed_predictions%ROWTYPE;
    v_task public.tasks%ROWTYPE;
    v_task_exists boolean := false;
    v_project uuid;
    v_manager jsonb;
    v_scope jsonb;
    v_source_extra jsonb;
    v_retry_job_id uuid;
    v_pending boolean;
    v_digest text;
    v_jobs jsonb;
    v_accepted_input_id uuid;
BEGIN
    IF p_payload IS NULL OR pg_catalog.octet_length(p_payload::text)>2097152
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','failed_prediction_id','actor_id','execution_queue','legacy_delivery']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','failed_prediction_id','actor_id','execution_queue','legacy_delivery'])
       OR pg_catalog.jsonb_typeof(p_payload -> 'job_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'job_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'failed_prediction_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'failed_prediction_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'actor_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'actor_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'execution_queue') NOT IN ('string','null')
       OR (p_payload -> 'execution_queue'<>'null'::jsonb AND pg_catalog.octet_length(p_payload ->> 'execution_queue') NOT BETWEEN 1 AND 256)
       OR pg_catalog.jsonb_typeof(p_payload -> 'legacy_delivery') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_request');
    END IF;
    v_job := (p_payload ->> 'job_id')::uuid;
    v_failed_id := (p_payload ->> 'failed_prediction_id')::uuid;
    v_actor := (p_payload ->> 'actor_id')::uuid;
    v_queue := CASE WHEN p_payload -> 'execution_queue'='null'::jsonb THEN NULL ELSE p_payload ->> 'execution_queue' END;
    v_legacy := (p_payload ->> 'legacy_delivery')::boolean;
    SELECT * INTO v_existing FROM public.prediction_retry_attempts WHERE job_id=v_job;
    IF FOUND THEN
        IF v_existing.failed_prediction_id IS DISTINCT FROM v_failed_id
           OR v_existing.actor_id IS DISTINCT FROM v_actor
           OR v_existing.execution_queue IS DISTINCT FROM v_queue
           OR v_existing.legacy_delivery IS DISTINCT FROM v_legacy THEN
            RETURN aap_labeling_v1._prediction_retry_reply(NULL,'scope_mismatch');
        END IF;
        RETURN aap_labeling_v1._prediction_retry_reply(
            pg_catalog.jsonb_build_object(
                'attempt',aap_labeling_v1._prediction_retry_attempt_json(v_existing),'created',false
            ),NULL
        );
    END IF;
    SELECT failed.project_id INTO v_project FROM public.failed_predictions AS failed
    WHERE failed.id=v_failed_id;
    IF NOT FOUND THEN RETURN aap_labeling_v1._prediction_retry_reply(NULL,'not_found'); END IF;
    v_manager := aap_workspace_v1.lock_project_manager(v_actor,v_project);
    IF v_manager ->> 'ok' IS DISTINCT FROM 'true' THEN
        IF v_manager #>> '{error,code}'='account_not_active' THEN
            RETURN aap_labeling_v1._prediction_retry_reply(NULL,'account_not_active');
        ELSIF v_manager #>> '{error,code}' IN ('account_busy','project_busy','membership_busy') THEN
            RETURN aap_labeling_v1._prediction_retry_reply(NULL,'execution_busy');
        END IF;
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'permission_denied');
    END IF;
    BEGIN
        SELECT * INTO v_failed FROM public.failed_predictions AS failed
        WHERE failed.id=v_failed_id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'execution_busy');
    END;
    IF NOT FOUND THEN RETURN aap_labeling_v1._prediction_retry_reply(NULL,'not_found'); END IF;
    IF v_failed.project_id IS DISTINCT FROM v_project THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'scope_mismatch');
    END IF;
    IF v_failed.dismissed_at IS NOT NULL THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'dismissed');
    END IF;
    IF v_failed.retry_count IS NULL OR v_failed.retry_count<0 THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_response');
    END IF;
    IF v_failed.retry_count>=3 THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'retry_limit');
    END IF;
    v_source_extra := CASE WHEN pg_catalog.jsonb_typeof(v_failed.extra)='object'
        THEN v_failed.extra ELSE '{}'::jsonb END;
    IF pg_catalog.octet_length(v_source_extra::text)>2000000
       OR NOT aap_labeling_v1._prediction_retry_no_raw_data(v_source_extra) THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_response');
    END IF;
    IF v_source_extra ? 'retry_pending' AND pg_catalog.jsonb_typeof(v_source_extra -> 'retry_pending') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_response');
    END IF;
    IF v_source_extra ? 'retry_job_id' AND v_source_extra -> 'retry_job_id'<>'null'::jsonb
       AND (pg_catalog.jsonb_typeof(v_source_extra -> 'retry_job_id') IS DISTINCT FROM 'string'
            OR v_source_extra ->> 'retry_job_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_response');
    END IF;
    v_pending := COALESCE((v_source_extra ->> 'retry_pending')::boolean,false);
    v_retry_job_id := CASE WHEN v_source_extra -> 'retry_job_id' IS NULL OR v_source_extra -> 'retry_job_id'='null'::jsonb
        THEN NULL ELSE (v_source_extra ->> 'retry_job_id')::uuid END;
    IF (v_pending AND v_retry_job_id IS NOT NULL)
       OR (NOT v_pending AND v_retry_job_id IS NOT NULL) THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'already_pending');
    END IF;
    IF v_pending AND NOT v_legacy THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'already_pending');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.prediction_retry_attempts AS attempt
        WHERE attempt.failed_prediction_id=v_failed_id
          AND attempt.state IN ('accepted','applying','failing')
    ) THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'already_pending');
    END IF;
    v_scope := NULL;
    IF v_failed.task_id IS NOT NULL THEN
        BEGIN
            SELECT * INTO v_task FROM public.tasks AS task
            WHERE task.id=v_failed.task_id FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_labeling_v1._prediction_retry_reply(NULL,'execution_busy');
        END;
        v_task_exists := FOUND;
        IF v_task_exists THEN
            IF v_task.project_id IS DISTINCT FROM v_failed.project_id THEN
                RETURN aap_labeling_v1._prediction_retry_reply(NULL,'scope_mismatch');
            END IF;
            IF v_source_extra #> '{accepted_input,input_id}' IS NOT NULL
               AND v_source_extra #> '{accepted_input,input_id}'<>'null'::jsonb THEN
                IF pg_catalog.jsonb_typeof(v_source_extra #> '{accepted_input,input_id}') IS DISTINCT FROM 'string'
                   OR v_source_extra #>> '{accepted_input,input_id}' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                    RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_response');
                END IF;
                v_accepted_input_id := (v_source_extra #>> '{accepted_input,input_id}')::uuid;
            END IF;
            v_scope := pg_catalog.jsonb_build_object(
                'task_id',v_task.id,'project_id',v_task.project_id,'actor_id',v_actor,
                'actor_known',true,'operation','prediction_retry','execution_scope',NULL,
                'execution_scope_known',true,'task_version',v_task.version,'task_status',v_task.status,
                'assignee_id',v_task.assignee_id,'review_round_id',v_task.review_round_id,
                'source_annotation_id',NULL,'source_annotation_version',NULL,
                'expected_batch_id',v_task.batch_id,'expected_batch_id_known',true,
                'job_id',v_job,'job_id_known',true,'source_binding_known',true,
                'task_source_generation',GREATEST(COALESCE(v_task.media_source_generation,1),1),
                'source_dataset_item_id',v_task.dataset_item_id,
                'source_file_path',v_task.file_path,'source_file_type',v_task.file_type,
                'accepted_input_id',v_accepted_input_id
            );
        END IF;
    END IF;
    IF EXISTS (SELECT 1 FROM public.async_jobs WHERE id=v_job)
       OR EXISTS (SELECT 1 FROM public.batch_result_executions WHERE job_id=v_job) THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'result_conflict');
    END IF;
    v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object(
            'job_id',v_job,'failed_prediction_id',v_failed.id,'actor_id',v_actor,
            'project_id',v_failed.project_id,'task_id',v_failed.task_id,
            'ml_backend_id',v_failed.ml_backend_id,'ml_backend_pool_id',v_failed.ml_backend_pool_id,
            'source_extra',v_source_extra,'retry_count_at_admission',v_failed.retry_count,
            'write_scope',v_scope,'execution_queue',v_queue,'legacy_delivery',v_legacy
        )::text,'UTF8'
    )),'hex');
    BEGIN
        INSERT INTO public.prediction_retry_attempts(
            job_id,failed_prediction_id,actor_id,project_id,task_id,ml_backend_id,
            ml_backend_pool_id,source_extra,retry_count_at_admission,write_scope,
            request_digest,execution_queue,legacy_delivery,state
        ) VALUES (
            v_job,v_failed.id,v_actor,v_failed.project_id,v_failed.task_id,
            v_failed.ml_backend_id,v_failed.ml_backend_pool_id,v_source_extra,
            v_failed.retry_count,v_scope,v_digest,v_queue,v_legacy,'accepted'
        ) RETURNING * INTO v_existing;
        UPDATE public.failed_predictions AS failed
        SET extra=v_source_extra || pg_catalog.jsonb_build_object('retry_pending',true,'retry_job_id',v_job)
        WHERE failed.id=v_failed.id;
        v_jobs := aap_jobs_v1._create_prediction_retry_job(
            pg_catalog.jsonb_build_object(
                'job_id',v_job,'failed_prediction_id',v_failed.id,'actor_id',v_actor,
                'project_id',v_failed.project_id,'task_id',CASE WHEN v_task_exists THEN v_task.id ELSE NULL END,
                'request_digest',v_digest,'execution_queue',v_queue,'write_scope',v_scope
            )
        );
        IF v_jobs -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE=COALESCE(v_jobs #>> '{error,code}','invalid_response');
        END IF;
        RETURN aap_labeling_v1._prediction_retry_reply(
            pg_catalog.jsonb_build_object(
                'attempt',aap_labeling_v1._prediction_retry_attempt_json(v_existing),'created',true
            ),NULL
        );
    EXCEPTION WHEN unique_violation THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'already_pending');
    WHEN OTHERS THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,CASE
            WHEN SQLERRM IN ('execution_busy','already_pending','retry_limit','dismissed','permission_denied','account_not_active','scope_mismatch','result_conflict')
                THEN SQLERRM ELSE 'invalid_response' END);
    END;
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.admit_prediction_retry(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.read_prediction_retry(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_attempt public.prediction_retry_attempts%ROWTYPE;
BEGIN
    IF p_payload IS NULL OR pg_catalog.octet_length(p_payload::text)>2097152
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','failed_prediction_id','actor_id']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','failed_prediction_id','actor_id'])
       OR pg_catalog.jsonb_typeof(p_payload -> 'job_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_payload -> 'failed_prediction_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_payload -> 'actor_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'job_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR p_payload ->> 'failed_prediction_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR p_payload ->> 'actor_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_request');
    END IF;
    SELECT * INTO v_attempt FROM public.prediction_retry_attempts
    WHERE job_id=(p_payload ->> 'job_id')::uuid;
    IF NOT FOUND THEN RETURN aap_labeling_v1._prediction_retry_reply(NULL,'not_found'); END IF;
    IF v_attempt.failed_prediction_id IS DISTINCT FROM (p_payload ->> 'failed_prediction_id')::uuid
       OR v_attempt.actor_id IS DISTINCT FROM (p_payload ->> 'actor_id')::uuid THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'scope_mismatch');
    END IF;
    RETURN aap_labeling_v1._prediction_retry_reply(
        pg_catalog.jsonb_build_object('attempt',aap_labeling_v1._prediction_retry_attempt_json(v_attempt)),NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_prediction_retry(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.claim_prediction_retry_deliveries(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_worker uuid;
    v_job uuid;
    v_limit integer;
    v_seconds integer;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_item public.prediction_retry_deliveries%ROWTYPE;
    v_items jsonb := '[]'::jsonb;
BEGIN
    IF p_payload IS NULL OR pg_catalog.octet_length(p_payload::text)>2097152
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['worker_id','job_id','limit','lease_seconds']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['worker_id','job_id','limit','lease_seconds'])
       OR pg_catalog.jsonb_typeof(p_payload -> 'worker_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'worker_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'job_id') NOT IN ('string','null')
       OR (p_payload -> 'job_id'<>'null'::jsonb AND p_payload ->> 'job_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
       OR pg_catalog.jsonb_typeof(p_payload -> 'limit') IS DISTINCT FROM 'number'
       OR p_payload ->> 'limit' !~ '^[0-9]+$'
       OR pg_catalog.char_length(p_payload ->> 'limit')>3
       OR pg_catalog.jsonb_typeof(p_payload -> 'lease_seconds') IS DISTINCT FROM 'number'
       OR p_payload ->> 'lease_seconds' !~ '^[0-9]+$'
       OR pg_catalog.char_length(p_payload ->> 'lease_seconds')>3 THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'invalid_request');
    END IF;
    v_worker := (p_payload ->> 'worker_id')::uuid;
    v_job := CASE WHEN p_payload -> 'job_id'='null'::jsonb THEN NULL ELSE (p_payload ->> 'job_id')::uuid END;
    v_limit := (p_payload ->> 'limit')::integer;
    v_seconds := (p_payload ->> 'lease_seconds')::integer;
    IF v_limit NOT BETWEEN 1 AND 100 OR v_seconds NOT BETWEEN 5 AND 300 THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'invalid_request');
    END IF;
    FOR v_item IN
        SELECT delivery.*
        FROM public.prediction_retry_deliveries AS delivery
        JOIN public.async_jobs AS job ON job.id=delivery.job_id
        LEFT JOIN public.batch_result_executions AS execution ON execution.job_id=delivery.job_id
        WHERE (v_job IS NULL OR delivery.job_id=v_job)
          AND job.kind='prediction_retry'
          AND job.status IN ('pending','running')
          AND job.user_id=delivery.actor_id
          AND COALESCE(job.project_id,job.retained_project_id)=delivery.project_id
          AND job.payload ->> 'failed_prediction_id'=delivery.failed_prediction_id::text
          AND job.payload ->> 'prediction_retry_request_digest'=execution.request_digest
          AND execution.actor_id=delivery.actor_id
          AND execution.project_id=delivery.project_id
          AND delivery.next_attempt_at<=v_now
          AND (
              delivery.state='pending'
              OR (delivery.state='claimed' AND delivery.lease_expires_at<=v_now)
              OR delivery.state='submitted'
          )
          AND NOT (execution.lease_token IS NOT NULL AND execution.lease_expires_at>v_now
                   AND execution.state IN ('preparing','running'))
        ORDER BY delivery.next_attempt_at,delivery.created_at,delivery.job_id
        LIMIT v_limit
        FOR UPDATE OF job,delivery SKIP LOCKED
    LOOP
        IF NOT aap_labeling_v1._prediction_retry_attempt_scope_valid(
            pg_catalog.jsonb_build_object(
                'job_id',v_item.job_id,'actor_id',v_item.actor_id,
                'project_id',v_item.project_id,
                'request_digest',(SELECT execution.request_digest FROM public.batch_result_executions AS execution WHERE execution.job_id=v_item.job_id)
            ),false
        ) THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='scope_mismatch';
        END IF;
        IF v_item.generation>=9223372036854775807 THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='invalid_response';
        END IF;
        UPDATE public.prediction_retry_deliveries AS delivery
        SET state='claimed',generation=delivery.generation+1,worker_id=v_worker,
            lease_token=pg_catalog.gen_random_uuid(),
            lease_expires_at=v_now+pg_catalog.make_interval(secs=>v_seconds),
            next_attempt_at=v_now+pg_catalog.make_interval(secs=>v_seconds),
            last_error_code=NULL,updated_at=v_now
        WHERE delivery.job_id=v_item.job_id
        RETURNING * INTO v_item;
        v_items := v_items || pg_catalog.jsonb_build_array(
            aap_jobs_v1._prediction_retry_delivery_json(v_item)
        );
    END LOOP;
    RETURN aap_jobs_v1._prediction_retry_reply(pg_catalog.jsonb_build_object('items',v_items),NULL);
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    RETURN aap_jobs_v1._prediction_retry_reply(NULL,CASE
        WHEN SQLERRM IN ('invalid_request','invalid_response','not_found','permission_denied',
            'account_not_active','already_pending','retry_limit','dismissed','scope_mismatch',
            'execution_busy','lease_lost','job_not_writable','result_conflict','result_not_ready',
            'storage_unavailable','content_deleting','content_epoch_changed') THEN SQLERRM
        ELSE 'invalid_response' END);
WHEN OTHERS THEN
    RETURN aap_jobs_v1._prediction_retry_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.claim_prediction_retry_deliveries(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.finish_prediction_retry_delivery(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_job uuid;
    v_generation bigint;
    v_token uuid;
    v_submitted boolean;
    v_error text;
    v_delivery public.prediction_retry_deliveries%ROWTYPE;
BEGIN
    IF p_payload IS NULL OR pg_catalog.octet_length(p_payload::text)>2097152
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','generation','lease_token','submitted','error_code']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','generation','lease_token','submitted','error_code'])
       OR pg_catalog.jsonb_typeof(p_payload -> 'job_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'job_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'generation') IS DISTINCT FROM 'number'
       OR p_payload ->> 'generation' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'generation')>19
       OR (p_payload ->> 'generation')::numeric>9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'lease_token') IS DISTINCT FROM 'string'
       OR p_payload ->> 'lease_token' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'submitted') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_payload -> 'error_code') NOT IN ('string','null')
       OR (p_payload -> 'error_code'<>'null'::jsonb AND pg_catalog.octet_length(p_payload ->> 'error_code') NOT BETWEEN 1 AND 256) THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'invalid_request');
    END IF;
    v_job := (p_payload ->> 'job_id')::uuid;
    v_generation := (p_payload ->> 'generation')::bigint;
    v_token := (p_payload ->> 'lease_token')::uuid;
    v_submitted := (p_payload ->> 'submitted')::boolean;
    v_error := CASE WHEN p_payload -> 'error_code'='null'::jsonb THEN NULL ELSE p_payload ->> 'error_code' END;
    SELECT * INTO v_delivery FROM public.prediction_retry_deliveries WHERE job_id=v_job FOR UPDATE;
    IF NOT FOUND THEN RETURN aap_jobs_v1._prediction_retry_reply(NULL,'not_found'); END IF;
    IF v_delivery.generation=v_generation AND v_delivery.lease_token=v_token
       AND v_delivery.state='submitted' AND v_submitted THEN
        RETURN aap_jobs_v1._prediction_retry_reply(pg_catalog.jsonb_build_object('job_id',v_job,'submitted',true),NULL);
    END IF;
    IF v_delivery.generation IS DISTINCT FROM v_generation
       OR v_delivery.lease_token IS DISTINCT FROM v_token
       OR v_delivery.state IS DISTINCT FROM 'claimed'
       OR v_delivery.lease_expires_at IS NULL
       OR v_delivery.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'lease_lost');
    END IF;
    IF v_submitted THEN
        UPDATE public.prediction_retry_deliveries
        SET state='submitted',worker_id=NULL,lease_expires_at=NULL,
            submitted_at=pg_catalog.clock_timestamp(),
            next_attempt_at=pg_catalog.clock_timestamp()+interval '30 seconds',
            last_error_code=NULL,updated_at=pg_catalog.clock_timestamp()
        WHERE job_id=v_job;
    ELSE
        UPDATE public.prediction_retry_deliveries
        SET state='pending',worker_id=NULL,lease_expires_at=NULL,
            next_attempt_at=pg_catalog.clock_timestamp()+interval '5 seconds',
            last_error_code=COALESCE(v_error,'publication_uncertain'),updated_at=pg_catalog.clock_timestamp()
        WHERE job_id=v_job;
    END IF;
    RETURN aap_jobs_v1._prediction_retry_reply(
        pg_catalog.jsonb_build_object('job_id',v_job,'submitted',v_submitted),NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._prediction_retry_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.finish_prediction_retry_delivery(jsonb) FROM PUBLIC;
-- aap-statement-break
