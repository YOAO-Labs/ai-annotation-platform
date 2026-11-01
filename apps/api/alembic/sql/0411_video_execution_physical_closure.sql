-- Preserve the public RPC identity while fencing settled release.
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
    v_bound_state text;
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
        -- A terminal Job is no-work only after the owner proves all of its
        -- execution artifacts closed under the same Job/execution locks.
        IF (v_terminal_cleanup OR v_job.status = 'cancelled')
           AND aap_jobs_v1._video_execution_settled(v_job_id, v_execution.run_id,
                                                   v_job.status, v_execution.realm,
                                                   v_execution.route_request_id) THEN
            -- Release does not require an unexpired lease: invalidate even an
            -- expired worker's token before returning without a new claim.
            UPDATE aap_jobs_v1.video_executions
            SET lease_token_hash = public.hmac(
                    pg_catalog.convert_to(public.gen_random_uuid()::text, 'UTF8'),
                    v_secret, 'sha256'
                ), updated_at = v_now
            WHERE job_id = v_job_id;
            RETURN aap_jobs_v1._video_execution_reply(
                pg_catalog.jsonb_build_object('settled',
                    pg_catalog.jsonb_build_object('job_id', v_job_id))
            );
        END IF;
        IF v_execution.generation >= 9223372036854775807 THEN
            RETURN aap_jobs_v1._video_execution_reply(NULL, 'job_not_active');
        END IF;
        IF v_execution.route_request_id IS NOT NULL THEN
            SELECT intent.state INTO v_bound_state
            FROM aap_jobs_v1.video_execution_route_intents AS intent
            WHERE intent.request_id = v_execution.route_request_id
              AND intent.job_id = v_job_id;
            v_bound := NOT FOUND OR v_bound_state = 'bound';
        END IF;
        v_run_id := v_execution.run_id;
        v_token := public.gen_random_uuid();
        v_generation := v_execution.generation + 1;
        v_expires := v_now + v_seconds * interval '1 second';
        v_state := CASE
            WHEN v_bound OR v_cancel_cleanup OR v_terminal_cleanup THEN 'recovery_required'
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
        -- A terminal claimant may release retained route history only when the
        -- same owner proof used for settled redelivery is complete.  Rotate the
        -- token even for an expired claim so it cannot release or write again.
        IF v_job.status IN ('cancelled','completed','failed','pending_review',
                            'partially_reviewed','accepted','discarded') THEN
            IF aap_jobs_v1._video_execution_settled(
                v_job_id, v_execution.run_id, v_job.status, v_execution.realm,
                v_execution.route_request_id
            ) IS DISTINCT FROM true THEN
                RETURN aap_jobs_v1._video_execution_reply(NULL, 'recovery_required');
            END IF;
            UPDATE aap_jobs_v1.video_executions
            SET lease_expires_at = LEAST(lease_expires_at, v_now),
                lease_token_hash = public.hmac(
                    pg_catalog.convert_to(public.gen_random_uuid()::text, 'UTF8'),
                    v_secret, 'sha256'
                ), updated_at = v_now
            WHERE job_id = v_job_id;
            RETURN aap_jobs_v1._video_execution_reply(
                pg_catalog.jsonb_build_object('released', true)
            );
        END IF;
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
            lease_token_hash = public.hmac(
                pg_catalog.convert_to(public.gen_random_uuid()::text, 'UTF8'),
                v_secret, 'sha256'
            ), route_request_id = NULL, updated_at = v_now
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
