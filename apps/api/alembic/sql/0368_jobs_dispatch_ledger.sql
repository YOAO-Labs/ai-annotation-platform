CREATE TABLE aap_jobs_v1.dispatch_intents (
    intent_id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES public.async_jobs(id) ON DELETE CASCADE,
    purpose text NOT NULL CHECK (purpose IN ('execution', 'frame_canvas', 'tracker_retirement')),
    execution_id text NOT NULL CHECK (execution_id <> ''),
    envelope jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(envelope) = 'object'),
    natural_identity boolean NOT NULL,
    state text NOT NULL CHECK (state IN ('leased', 'pending', 'submitted', 'retired')),
    lease_token uuid,
    generation bigint NOT NULL CHECK (generation > 0),
    attempts integer NOT NULL CHECK (attempts > 0),
    lease_expires_at timestamp with time zone,
    next_attempt_at timestamp with time zone,
    accepted_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL DEFAULT pg_catalog.transaction_timestamp(),
    updated_at timestamp with time zone NOT NULL DEFAULT pg_catalog.transaction_timestamp(),
    CONSTRAINT ck_jobs_dispatch_lease_state CHECK (
        (state = 'leased' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (state <> 'leased' AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CONSTRAINT ck_jobs_dispatch_submitted_state CHECK (
        (state = 'submitted' AND accepted_at IS NOT NULL)
        OR (state <> 'submitted' AND accepted_at IS NULL)
    ),
    CONSTRAINT ck_jobs_dispatch_retry_state CHECK (
        (state = 'pending' AND next_attempt_at IS NOT NULL)
        OR (state <> 'pending' AND next_attempt_at IS NULL)
    )
);
-- aap-statement-break
CREATE UNIQUE INDEX uq_jobs_dispatch_natural_identity
    ON aap_jobs_v1.dispatch_intents(job_id, purpose, execution_id)
    WHERE natural_identity;
-- aap-statement-break
CREATE INDEX ix_jobs_dispatch_claimable
    ON aap_jobs_v1.dispatch_intents(state, next_attempt_at, lease_expires_at, created_at)
    WHERE state IN ('pending', 'leased');
-- aap-statement-break
REVOKE ALL ON TABLE aap_jobs_v1.dispatch_intents FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.prepare_dispatch(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_intent_id uuid;
    v_natural boolean;
    v_job_id uuid;
    v_purpose text;
    v_execution_id text;
    v_envelope jsonb;
    v_job_kind text;
    v_job_status text;
    v_tracker_status text;
    v_dispatch aap_jobs_v1.dispatch_intents%ROWTYPE;
    v_existing boolean := false;
    v_now timestamp with time zone;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR NOT (p_request ?& ARRAY['intent_id', 'job_id', 'purpose', 'envelope']::text[])
       OR (p_request - ARRAY['intent_id', 'job_id', 'purpose', 'envelope']::text[]) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_request -> 'job_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request -> 'purpose') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request -> 'envelope') IS DISTINCT FROM 'object'
       OR (p_request -> 'intent_id') IS DISTINCT FROM 'null'::jsonb
          AND pg_catalog.jsonb_typeof(p_request -> 'intent_id') IS DISTINCT FROM 'string' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    BEGIN
        v_job_id := (p_request ->> 'job_id')::uuid;
        IF pg_catalog.jsonb_typeof(p_request -> 'intent_id') = 'string' THEN
            v_intent_id := (p_request ->> 'intent_id')::uuid;
        END IF;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END;
    v_purpose := p_request ->> 'purpose';
    v_envelope := p_request -> 'envelope';
    v_execution_id := v_envelope ->> 'execution_id';
    IF v_purpose NOT IN ('execution', 'frame_canvas', 'tracker_retirement')
       OR v_envelope ->> 'schema' IS DISTINCT FROM 'aap-jobs-queue/v2'
       OR (v_envelope ->> 'operation' IS DISTINCT FROM 'dispatch'
           AND v_envelope ->> 'operation' IS DISTINCT FROM 'canvas')
       OR pg_catalog.jsonb_typeof(v_envelope -> 'execution_id') IS DISTINCT FROM 'string'
       OR v_execution_id IS NULL OR v_execution_id = '' OR pg_catalog.length(v_execution_id) > 64
       OR (
           v_envelope ->> 'operation' = 'dispatch'
           AND (
               NOT (v_envelope ?& ARRAY[
                   'schema', 'operation', 'task_name', 'execution_id', 'args', 'kwargs', 'options'
               ]::text[])
               OR (v_envelope - ARRAY[
                   'schema', 'operation', 'task_name', 'execution_id', 'args', 'kwargs', 'options'
               ]::text[]) <> '{}'::jsonb
               OR pg_catalog.jsonb_typeof(v_envelope -> 'task_name') IS DISTINCT FROM 'string'
               OR v_envelope ->> 'task_name' = ''
               OR pg_catalog.jsonb_typeof(v_envelope -> 'args') IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_typeof(v_envelope -> 'kwargs') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_envelope -> 'options') IS DISTINCT FROM 'object'
               OR ((v_envelope -> 'options') - ARRAY['queue', 'soft_time_limit']::text[]) <> '{}'::jsonb
               OR (
                   (v_envelope -> 'options') ? 'queue'
                   AND (
                       pg_catalog.jsonb_typeof(v_envelope -> 'options' -> 'queue') IS DISTINCT FROM 'string'
                       OR v_envelope -> 'options' ->> 'queue' = ''
                   )
               )
               OR (
                   (v_envelope -> 'options') ? 'soft_time_limit'
                   AND (
                       pg_catalog.jsonb_typeof(v_envelope -> 'options' -> 'soft_time_limit') IS DISTINCT FROM 'number'
                       OR v_envelope -> 'options' -> 'soft_time_limit' <= '0'::jsonb
                   )
               )
           )
       )
       OR (
           v_envelope ->> 'operation' = 'canvas'
           AND (
               NOT (v_envelope ?& ARRAY[
                   'schema', 'operation', 'execution_id', 'group_id', 'tasks', 'callback'
               ]::text[])
               OR (v_envelope - ARRAY[
                   'schema', 'operation', 'execution_id', 'group_id', 'tasks', 'callback'
               ]::text[]) <> '{}'::jsonb
               OR pg_catalog.jsonb_typeof(v_envelope -> 'group_id') IS DISTINCT FROM 'string'
               OR v_envelope ->> 'group_id' = ''
               OR pg_catalog.length(v_envelope ->> 'group_id') > 64
               OR pg_catalog.jsonb_typeof(v_envelope -> 'tasks') IS DISTINCT FROM 'array'
               OR v_envelope ->> 'group_id' = v_execution_id
               OR CASE WHEN pg_catalog.jsonb_typeof(v_envelope -> 'tasks') = 'array' THEN
                   pg_catalog.jsonb_array_length(v_envelope -> 'tasks') = 0
                   OR (
                       SELECT pg_catalog.count(DISTINCT task ->> 'execution_id')
                       FROM pg_catalog.jsonb_array_elements(v_envelope -> 'tasks') AS task
                   ) <> pg_catalog.jsonb_array_length(v_envelope -> 'tasks')
                   OR EXISTS (
                       SELECT FROM pg_catalog.jsonb_array_elements(v_envelope -> 'tasks') AS task
                       WHERE task ->> 'execution_id' IN (
                           v_execution_id, v_envelope ->> 'group_id'
                       )
                   )
               ELSE false END
               OR pg_catalog.jsonb_typeof(v_envelope -> 'callback') IS DISTINCT FROM 'object'
               OR v_envelope -> 'callback' ->> 'execution_id' IS DISTINCT FROM v_execution_id
               OR pg_catalog.jsonb_typeof(v_envelope -> 'callback' -> 'execution_id') IS DISTINCT FROM 'string'
               OR NOT ((v_envelope -> 'callback') ?& ARRAY[
                   'task_name', 'execution_id', 'args', 'kwargs', 'options'
               ]::text[])
               OR (
                   (v_envelope -> 'callback') - ARRAY[
                       'task_name', 'execution_id', 'args', 'kwargs', 'options'
                   ]::text[]
               ) <> '{}'::jsonb
               OR pg_catalog.jsonb_typeof(v_envelope -> 'callback' -> 'task_name') IS DISTINCT FROM 'string'
               OR v_envelope -> 'callback' ->> 'task_name' = ''
               OR pg_catalog.jsonb_typeof(v_envelope -> 'callback' -> 'args') IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_typeof(v_envelope -> 'callback' -> 'kwargs') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_envelope -> 'callback' -> 'options') IS DISTINCT FROM 'object'
               OR ((v_envelope -> 'callback' -> 'options') - ARRAY['queue', 'soft_time_limit']::text[]) <> '{}'::jsonb
               OR (
                   (v_envelope -> 'callback' -> 'options') ? 'queue'
                   AND (
                       pg_catalog.jsonb_typeof(v_envelope -> 'callback' -> 'options' -> 'queue') IS DISTINCT FROM 'string'
                       OR v_envelope -> 'callback' -> 'options' ->> 'queue' = ''
                   )
               )
               OR (
                   (v_envelope -> 'callback' -> 'options') ? 'soft_time_limit'
                   AND (
                       pg_catalog.jsonb_typeof(v_envelope -> 'callback' -> 'options' -> 'soft_time_limit') IS DISTINCT FROM 'number'
                       OR v_envelope -> 'callback' -> 'options' -> 'soft_time_limit' <= '0'::jsonb
                   )
               )
               OR CASE
                   WHEN pg_catalog.jsonb_typeof(v_envelope -> 'tasks') = 'array' THEN EXISTS (
                       SELECT FROM pg_catalog.jsonb_array_elements(v_envelope -> 'tasks') AS task(spec)
                       WHERE pg_catalog.jsonb_typeof(task.spec) IS DISTINCT FROM 'object'
                          OR NOT (task.spec ?& ARRAY[
                              'task_name', 'execution_id', 'args', 'kwargs', 'options'
                          ]::text[])
                          OR (task.spec - ARRAY[
                              'task_name', 'execution_id', 'args', 'kwargs', 'options'
                          ]::text[]) <> '{}'::jsonb
                          OR pg_catalog.jsonb_typeof(task.spec -> 'task_name') IS DISTINCT FROM 'string'
                          OR task.spec ->> 'task_name' = ''
                          OR pg_catalog.jsonb_typeof(task.spec -> 'execution_id') IS DISTINCT FROM 'string'
                          OR task.spec ->> 'execution_id' = ''
                          OR pg_catalog.length(task.spec ->> 'execution_id') > 64
                          OR pg_catalog.jsonb_typeof(task.spec -> 'args') IS DISTINCT FROM 'array'
                          OR pg_catalog.jsonb_typeof(task.spec -> 'kwargs') IS DISTINCT FROM 'object'
                          OR pg_catalog.jsonb_typeof(task.spec -> 'options') IS DISTINCT FROM 'object'
                          OR ((task.spec -> 'options') - ARRAY['queue', 'soft_time_limit']::text[]) <> '{}'::jsonb
                          OR (
                              (task.spec -> 'options') ? 'queue'
                              AND (
                                  pg_catalog.jsonb_typeof(task.spec -> 'options' -> 'queue') IS DISTINCT FROM 'string'
                                  OR task.spec -> 'options' ->> 'queue' = ''
                              )
                          )
                          OR (
                              (task.spec -> 'options') ? 'soft_time_limit'
                              AND (
                                  pg_catalog.jsonb_typeof(task.spec -> 'options' -> 'soft_time_limit') IS DISTINCT FROM 'number'
                                  OR task.spec -> 'options' -> 'soft_time_limit' <= '0'::jsonb
                              )
                          )
                   )
                   ELSE true
               END
           )
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT job.kind, job.status
    INTO v_job_kind, v_job_status
    FROM public.async_jobs AS job
    WHERE job.id = v_job_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
            'error', pg_catalog.jsonb_build_object('code', 'job_missing')
        );
    END IF;

    v_natural := v_intent_id IS NULL;
    IF v_natural THEN
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
            'aap:jobs-dispatch:' || v_job_id::text || ':' || v_purpose || ':' || v_execution_id,
            0
        ));
        SELECT dispatch.* INTO v_dispatch
        FROM aap_jobs_v1.dispatch_intents AS dispatch
        WHERE dispatch.natural_identity
          AND dispatch.job_id = v_job_id
          AND dispatch.purpose = v_purpose
          AND dispatch.execution_id = v_execution_id
        FOR UPDATE;
    ELSE
        SELECT dispatch.* INTO v_dispatch
        FROM aap_jobs_v1.dispatch_intents AS dispatch
        WHERE dispatch.intent_id = v_intent_id
        FOR UPDATE;
    END IF;
    v_existing := FOUND;
    v_now := pg_catalog.clock_timestamp();
    IF v_existing THEN
        IF v_dispatch.job_id IS DISTINCT FROM v_job_id
           OR v_dispatch.purpose IS DISTINCT FROM v_purpose
           OR v_dispatch.execution_id IS DISTINCT FROM v_execution_id
           OR v_dispatch.envelope IS DISTINCT FROM v_envelope THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
                'error', pg_catalog.jsonb_build_object('code', 'identity_mismatch')
            );
        END IF;
        IF v_dispatch.state IN ('submitted', 'retired') THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', true,
                'data', pg_catalog.jsonb_build_object(
                    'intent_id', v_dispatch.intent_id,
                    'job_id', v_dispatch.job_id,
                    'purpose', v_dispatch.purpose,
                    'execution_id', v_dispatch.execution_id,
                    'envelope', v_dispatch.envelope,
                    'lease_token', NULL,
                    'generation', v_dispatch.generation,
                    'state', v_dispatch.state
                ),
                'error', NULL
            );
        END IF;
    END IF;

    IF v_purpose = 'frame_canvas' THEN
        IF v_job_status IS DISTINCT FROM 'running' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
                'error', pg_catalog.jsonb_build_object('code', 'job_not_dispatchable')
            );
        END IF;
    ELSIF v_purpose = 'tracker_retirement' THEN
        SELECT tracker.status INTO v_tracker_status
        FROM aap_labeling_v1.video_tracker_job_rows AS tracker
        WHERE tracker.id = v_job_id
          AND (
              (v_job_kind = 'video_tracker' AND tracker.job_kind = 'tracking')
              OR (v_job_kind = 'video_correction' AND tracker.job_kind = 'correction')
          );
        IF NOT FOUND OR NOT (
            (v_job_status IN ('pending', 'running') AND v_tracker_status IN ('queued', 'running'))
            OR (v_job_status = 'cancelled' AND v_tracker_status = 'cancelled')
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
                'error', pg_catalog.jsonb_build_object('code', 'tracker_retirement_not_allowed')
            );
        END IF;
        IF v_job_kind NOT IN ('video_tracker', 'video_correction')
           OR v_envelope ->> 'operation' IS DISTINCT FROM 'dispatch'
           OR v_envelope ->> 'task_name' IS DISTINCT FROM 'app.workers.video_tracker.run_video_tracker_job'
           OR v_envelope -> 'args' IS DISTINCT FROM pg_catalog.jsonb_build_array(v_job_id::text)
           OR v_envelope -> 'kwargs' IS DISTINCT FROM '{}'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
                'error', pg_catalog.jsonb_build_object('code', 'tracker_retirement_not_allowed')
            );
        END IF;
    ELSIF v_job_status NOT IN ('pending', 'running') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
            'error', pg_catalog.jsonb_build_object('code', 'job_not_dispatchable')
        );
    END IF;

    IF v_existing
       AND v_dispatch.state = 'pending'
       AND v_dispatch.next_attempt_at > v_now THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'intent_id', v_dispatch.intent_id,
                'job_id', v_dispatch.job_id,
                'purpose', v_dispatch.purpose,
                'execution_id', v_dispatch.execution_id,
                'envelope', v_dispatch.envelope,
                'lease_token', NULL,
                'generation', v_dispatch.generation,
                'state', v_dispatch.state
            ),
            'error', NULL
        );
    END IF;
    IF v_existing THEN
        IF v_dispatch.state = 'leased'
           AND v_dispatch.lease_expires_at > v_now THEN
            NULL;
        ELSE
            UPDATE aap_jobs_v1.dispatch_intents AS dispatch
            SET state = 'leased',
                lease_token = pg_catalog.gen_random_uuid(),
                generation = dispatch.generation + 1,
                attempts = dispatch.attempts + 1,
                lease_expires_at = v_now + INTERVAL '30 seconds',
                next_attempt_at = NULL,
                updated_at = v_now
            WHERE dispatch.intent_id = v_dispatch.intent_id
            RETURNING dispatch.* INTO v_dispatch;
        END IF;
    ELSE
        v_intent_id := COALESCE(v_intent_id, pg_catalog.gen_random_uuid());
        INSERT INTO aap_jobs_v1.dispatch_intents (
            intent_id, job_id, purpose, execution_id, envelope, natural_identity,
            state, lease_token, generation, attempts, lease_expires_at, next_attempt_at
        ) VALUES (
            v_intent_id, v_job_id, v_purpose, v_execution_id, v_envelope, v_natural,
            'leased', pg_catalog.gen_random_uuid(), 1, 1,
            v_now + INTERVAL '30 seconds', NULL
        )
        RETURNING * INTO v_dispatch;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'intent_id', v_dispatch.intent_id,
            'job_id', v_dispatch.job_id,
            'purpose', v_dispatch.purpose,
            'execution_id', v_dispatch.execution_id,
            'envelope', v_dispatch.envelope,
            'lease_token', v_dispatch.lease_token,
            'generation', v_dispatch.generation,
            'state', v_dispatch.state
        ),
        'error', NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.prepare_dispatch(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.claim_dispatches(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_limit integer;
    v_lease_seconds integer;
    v_now timestamp with time zone := pg_catalog.clock_timestamp();
    v_candidate record;
    v_dispatch aap_jobs_v1.dispatch_intents%ROWTYPE;
    v_leases jsonb := '[]'::jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR NOT (p_request ?& ARRAY['limit', 'lease_seconds']::text[])
       OR (p_request - ARRAY['limit', 'lease_seconds']::text[]) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_request -> 'limit') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(p_request -> 'lease_seconds') IS DISTINCT FROM 'number'
       OR (p_request ->> 'limit') !~ '^[0-9]+$'
       OR (p_request ->> 'lease_seconds') !~ '^[0-9]+$' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    BEGIN
        v_limit := (p_request ->> 'limit')::integer;
        v_lease_seconds := (p_request ->> 'lease_seconds')::integer;
    EXCEPTION WHEN numeric_value_out_of_range OR invalid_text_representation THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END;
    IF v_limit NOT BETWEEN 1 AND 500 OR v_lease_seconds NOT BETWEEN 1 AND 300 THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    FOR v_candidate IN
        SELECT dispatch.intent_id
        FROM aap_jobs_v1.dispatch_intents AS dispatch
        JOIN public.async_jobs AS job ON job.id = dispatch.job_id
        WHERE dispatch.state IN ('pending', 'leased')
          AND (dispatch.state = 'pending' OR dispatch.lease_expires_at <= v_now)
          AND NOT (
              (dispatch.purpose = 'execution' AND job.status IN ('pending', 'running'))
              OR (dispatch.purpose = 'frame_canvas' AND job.status = 'running')
              OR (
                  dispatch.purpose = 'tracker_retirement'
                  AND job.kind IN ('video_tracker', 'video_correction')
                  AND EXISTS (
                      SELECT FROM aap_labeling_v1.video_tracker_job_rows AS tracker
                      WHERE tracker.id = job.id
                        AND (
                            (job.kind = 'video_tracker' AND tracker.job_kind = 'tracking')
                            OR (job.kind = 'video_correction' AND tracker.job_kind = 'correction')
                        )
                        AND (
                            (job.status IN ('pending', 'running')
                             AND tracker.status IN ('queued', 'running'))
                            OR (job.status = 'cancelled' AND tracker.status = 'cancelled')
                        )
                  )
              )
          )
        ORDER BY dispatch.created_at, dispatch.intent_id
        LIMIT v_limit
        FOR UPDATE OF job SKIP LOCKED
    LOOP
        SELECT dispatch.* INTO v_dispatch
        FROM aap_jobs_v1.dispatch_intents AS dispatch
        WHERE dispatch.intent_id = v_candidate.intent_id
        FOR UPDATE SKIP LOCKED;
        IF FOUND AND v_dispatch.state IN ('pending', 'leased')
           AND (v_dispatch.state = 'pending' OR v_dispatch.lease_expires_at <= v_now) THEN
            UPDATE aap_jobs_v1.dispatch_intents
            SET state = 'retired', lease_token = NULL, lease_expires_at = NULL,
                next_attempt_at = NULL, updated_at = v_now
            WHERE intent_id = v_dispatch.intent_id;
        END IF;
    END LOOP;
    v_now := pg_catalog.clock_timestamp();

    FOR v_candidate IN
        SELECT dispatch.intent_id
        FROM aap_jobs_v1.dispatch_intents AS dispatch
        JOIN public.async_jobs AS job ON job.id = dispatch.job_id
        WHERE (
            (dispatch.state = 'pending' AND dispatch.next_attempt_at <= v_now)
            OR (dispatch.state = 'leased' AND dispatch.lease_expires_at <= v_now)
        )
          AND (
              (dispatch.purpose = 'execution' AND job.status IN ('pending', 'running'))
              OR (dispatch.purpose = 'frame_canvas' AND job.status = 'running')
              OR (
                  dispatch.purpose = 'tracker_retirement'
                  AND job.kind IN ('video_tracker', 'video_correction')
                  AND EXISTS (
                      SELECT FROM aap_labeling_v1.video_tracker_job_rows AS tracker
                      WHERE tracker.id = job.id
                        AND (
                            (job.kind = 'video_tracker' AND tracker.job_kind = 'tracking')
                            OR (job.kind = 'video_correction' AND tracker.job_kind = 'correction')
                        )
                        AND (
                            (job.status IN ('pending', 'running')
                             AND tracker.status IN ('queued', 'running'))
                            OR (job.status = 'cancelled' AND tracker.status = 'cancelled')
                        )
                  )
              )
          )
        ORDER BY COALESCE(dispatch.next_attempt_at, dispatch.lease_expires_at),
                 dispatch.created_at, dispatch.intent_id
        LIMIT v_limit
        FOR UPDATE OF job SKIP LOCKED
    LOOP
        SELECT dispatch.* INTO v_dispatch
        FROM aap_jobs_v1.dispatch_intents AS dispatch
        WHERE dispatch.intent_id = v_candidate.intent_id
        FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        v_now := pg_catalog.clock_timestamp();
        IF NOT (
            (v_dispatch.state = 'pending' AND v_dispatch.next_attempt_at <= v_now)
            OR (v_dispatch.state = 'leased' AND v_dispatch.lease_expires_at <= v_now)
        ) THEN
            CONTINUE;
        END IF;
        UPDATE aap_jobs_v1.dispatch_intents AS dispatch
        SET state = 'leased', lease_token = pg_catalog.gen_random_uuid(),
            generation = dispatch.generation + 1,
            attempts = dispatch.attempts + 1,
            lease_expires_at = v_now + pg_catalog.make_interval(secs => v_lease_seconds),
            next_attempt_at = NULL, updated_at = v_now
        WHERE dispatch.intent_id = v_candidate.intent_id
        RETURNING dispatch.* INTO v_dispatch;
        IF FOUND THEN
            v_leases := v_leases || pg_catalog.jsonb_build_array(
                pg_catalog.jsonb_build_object(
                    'intent_id', v_dispatch.intent_id,
                    'job_id', v_dispatch.job_id,
                    'purpose', v_dispatch.purpose,
                    'execution_id', v_dispatch.execution_id,
                    'envelope', v_dispatch.envelope,
                    'lease_token', v_dispatch.lease_token,
                    'generation', v_dispatch.generation,
                    'state', v_dispatch.state
                )
            );
        END IF;
    END LOOP;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', true,
        'data', v_leases, 'error', NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.claim_dispatches(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.finish_dispatch(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_intent_id uuid;
    v_lease_token uuid;
    v_generation bigint;
    v_accepted boolean;
    v_dispatch aap_jobs_v1.dispatch_intents%ROWTYPE;
    v_now timestamp with time zone;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR NOT (p_request ?& ARRAY['intent_id', 'lease_token', 'generation', 'accepted']::text[])
       OR (p_request - ARRAY['intent_id', 'lease_token', 'generation', 'accepted']::text[]) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_request -> 'intent_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request -> 'lease_token') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request -> 'generation') IS DISTINCT FROM 'number'
       OR (p_request ->> 'generation') !~ '^[0-9]+$'
       OR pg_catalog.jsonb_typeof(p_request -> 'accepted') IS DISTINCT FROM 'boolean' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    BEGIN
        v_intent_id := (p_request ->> 'intent_id')::uuid;
        v_lease_token := (p_request ->> 'lease_token')::uuid;
        v_generation := (p_request ->> 'generation')::bigint;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END;
    v_accepted := (p_request ->> 'accepted')::boolean;
    IF v_generation < 1 THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT dispatch.* INTO v_dispatch
    FROM aap_jobs_v1.dispatch_intents AS dispatch
    WHERE dispatch.intent_id = v_intent_id
    FOR UPDATE;
    IF NOT FOUND
       OR v_dispatch.state IS DISTINCT FROM 'leased'
       OR v_dispatch.lease_token IS DISTINCT FROM v_lease_token
       OR v_dispatch.generation IS DISTINCT FROM v_generation THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
            'error', pg_catalog.jsonb_build_object('code', 'stale_lease')
        );
    END IF;
    v_now := pg_catalog.clock_timestamp();
    UPDATE aap_jobs_v1.dispatch_intents AS dispatch
    SET state = CASE WHEN v_accepted THEN 'submitted' ELSE 'pending' END,
        lease_token = NULL,
        lease_expires_at = NULL,
        next_attempt_at = CASE WHEN v_accepted THEN NULL ELSE
            v_now + pg_catalog.make_interval(
                secs => LEAST(300::double precision,
                              pg_catalog.power(2::double precision,
                                               LEAST(dispatch.attempts, 8)))::integer
            ) END,
        accepted_at = CASE WHEN v_accepted THEN v_now ELSE NULL END,
        updated_at = v_now
    WHERE dispatch.intent_id = v_intent_id
      AND dispatch.state = 'leased'
      AND dispatch.lease_token = v_lease_token
      AND dispatch.generation = v_generation
    RETURNING dispatch.* INTO v_dispatch;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', false, 'data', NULL,
            'error', pg_catalog.jsonb_build_object('code', 'stale_lease')
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'intent_id', v_dispatch.intent_id,
            'job_id', v_dispatch.job_id,
            'purpose', v_dispatch.purpose,
            'execution_id', v_dispatch.execution_id,
            'state', v_dispatch.state,
            'generation', v_dispatch.generation,
            'attempts', v_dispatch.attempts
        ),
        'error', NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.finish_dispatch(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.dispatch_status(p_intent_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-jobs-dispatch-ledger/v1', 'ok', true,
        'data', (
            SELECT pg_catalog.jsonb_build_object(
                'intent_id', dispatch.intent_id,
                'job_id', dispatch.job_id,
                'purpose', dispatch.purpose,
                'execution_id', dispatch.execution_id,
                'state', dispatch.state,
                'generation', dispatch.generation,
                'attempts', dispatch.attempts
            )
            FROM aap_jobs_v1.dispatch_intents AS dispatch
            WHERE dispatch.intent_id = p_intent_id
        ),
        'error', NULL
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.dispatch_status(uuid) FROM PUBLIC;
