-- Keep the Jobs deletion fence and tracker-input GC on the same fail-closed
-- status proof. Execution retirement still requires a same-run journal.
CREATE OR REPLACE FUNCTION aap_jobs_v1.video_tracker_input_retention_facts(
    p_input_id uuid,
    p_tracker_job_id uuid
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
    v_asset_facts jsonb;
    v_job_id uuid;
    v_job_started_at timestamptz;
    v_job_status text;
    v_job_exists boolean;
    v_execution aap_jobs_v1.video_executions%ROWTYPE;
    v_execution_exists boolean;
    v_journal aap_jobs_v1.video_execution_journals%ROWTYPE;
    v_journal_exists boolean;
    v_clearance_exists boolean;
    v_execution_retained boolean;
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-video-tracker-input-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'execution_busy')
        );
    END IF;
    IF p_input_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-video-tracker-input-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_job_id := p_tracker_job_id;
    IF v_job_id IS NULL THEN
        v_asset_facts := aap_assets_v1.video_execution_input_facts(p_input_id);
        IF v_asset_facts IS NULL
           OR v_asset_facts ->> 'schema' IS DISTINCT FROM
                'aap-assets-video-execution-input-facts/v1'
           OR v_asset_facts -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR pg_catalog.jsonb_typeof(v_asset_facts -> 'data') IS DISTINCT FROM 'object'
           OR v_asset_facts #>> '{data,input_id}' IS DISTINCT FROM p_input_id::text
           OR pg_catalog.jsonb_typeof(v_asset_facts #> '{data,job_id}')
                IS DISTINCT FROM 'string'
           OR v_asset_facts #>> '{data,request_id}' IS DISTINCT FROM
                v_asset_facts #>> '{data,job_id}' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-jobs-video-tracker-input-retention/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'input_unavailable')
            );
        END IF;
        BEGIN
            v_job_id := aap_jobs_v1._video_execution_uuid(
                v_asset_facts #> '{data,job_id}'
            );
        EXCEPTION WHEN OTHERS THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-jobs-video-tracker-input-retention/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'input_unavailable')
            );
        END;
    END IF;

    -- Match the deletion writer's canonical Job -> execution -> journal order.
    SELECT job.started_at, job.status
    INTO v_job_started_at, v_job_status
    FROM public.video_tracker_jobs AS job
    WHERE job.id = v_job_id
    FOR SHARE NOWAIT;
    v_job_exists := FOUND;

    SELECT execution.* INTO v_execution
    FROM aap_jobs_v1.video_executions AS execution
    WHERE execution.job_id = v_job_id
    FOR SHARE NOWAIT;
    v_execution_exists := FOUND;

    SELECT journal.* INTO v_journal
    FROM aap_jobs_v1.video_execution_journals AS journal
    WHERE journal.job_id = v_job_id
    FOR SHARE NOWAIT;
    v_journal_exists := FOUND;

    IF v_execution_exists THEN
        v_execution_retained := NOT (
            v_journal_exists
            AND v_journal.run_id = v_execution.run_id
            AND v_journal.writer_terminal
            AND v_journal.execution_retired
        );
        IF NOT v_execution_retained AND v_job_exists
           AND NOT COALESCE(v_job_status = ANY (ARRAY[
                'completed', 'failed', 'cancelled', 'pending_review',
                'partially_reviewed', 'accepted', 'discarded'
           ]::text[]), false) THEN
            v_execution_retained := true;
        END IF;
    ELSIF v_journal_exists THEN
        v_execution_retained := true;
    ELSIF v_job_exists THEN
        v_execution_retained := v_job_started_at IS NOT NULL
            OR NOT COALESCE(v_job_status = ANY (ARRAY[
                'queued', 'cancelled', 'completed', 'failed', 'pending_review',
                'partially_reviewed', 'accepted', 'discarded'
            ]::text[]), false);
    ELSE
        SELECT EXISTS (
            SELECT 1
            FROM aap_jobs_v1.video_deletion_clearances AS clearance
            WHERE clearance.job_id = v_job_id
        ) INTO v_clearance_exists;
        v_execution_retained := NOT v_clearance_exists;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-jobs-video-tracker-input-retention/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'input_id', p_input_id,
            'tracker_job_id', v_job_id,
            'execution_retained', v_execution_retained
        )
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-video-tracker-input-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'execution_busy')
        );
    WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-jobs-video-tracker-input-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'input_unavailable')
        );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_jobs_v1.video_tracker_input_retention_facts(uuid, uuid) FROM PUBLIC;
-- aap-statement-break

-- A ready tracker Job consumes the immutable source Blob in its accepted plan.
-- Once that plan is published, expired inputs retain the same plan; superseded
-- attempt prefixes do not point at that frozen source and can still be cleaned.
CREATE OR REPLACE FUNCTION aap_assets_v1.claim_accepted_video_input_garbage(
    p_worker_id uuid, p_lease_seconds integer, p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item public.asset_accepted_input_garbage%ROWTYPE;
    v_candidate record;
    v_reply jsonb;
    v_labeling_reply jsonb;
    v_now timestamptz;
    v_expires timestamptz;
    v_token uuid;
    v_items jsonb := '[]'::jsonb;
    v_prefix_known boolean;
    v_current_root_count bigint;
    v_tracker_input_count bigint;
    v_current_tracker_input_id uuid;
    v_retained boolean;
BEGIN
    IF p_worker_id IS NULL OR p_lease_seconds NOT BETWEEN 60 AND 7200
       OR p_page_size NOT BETWEEN 1 AND 100 THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'invalid_request');
    END IF;
    v_reply := aap_assets_v1.sweep_accepted_video_inputs(p_page_size);
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-video-input-execution/v1'
       OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_reply #> '{data,expired}') IS DISTINCT FROM 'number' THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'input_busy');
    END IF;

    FOR v_candidate IN
        SELECT garbage.garbage_id
        FROM public.asset_accepted_input_garbage AS garbage
        WHERE aap_assets_v1._video_input_prefix_valid(garbage.prefix)
          AND garbage.not_before <= pg_catalog.clock_timestamp()
          AND (garbage.lease_token IS NULL
               OR garbage.lease_expires_at <= pg_catalog.clock_timestamp())
        ORDER BY garbage.not_before, garbage.garbage_id
        LIMIT p_page_size FOR UPDATE OF garbage SKIP LOCKED
    LOOP
        SELECT garbage.* INTO v_item
        FROM public.asset_accepted_input_garbage AS garbage
        WHERE garbage.garbage_id = v_candidate.garbage_id;
        IF NOT aap_assets_v1._video_input_prefix_valid(v_item.prefix) THEN
            RETURN aap_assets_v1._video_input_reply(NULL, 'invalid_response');
        END IF;
        v_now := pg_catalog.clock_timestamp();

        -- Ready and expired input plans and frame blobs are immutable. A later
        -- generation proves an attempt was superseded; a same-generation
        -- pointer mismatch is unknown and must use the owner retention fact.
        WITH prefix_roots AS (
            SELECT input.input_id, input.input_owner,
                (
                    input.plan IS NOT NULL
                    AND input.generation = attempt.generation
                    AND input.plan #>> '{input,blob,key}' = v_item.prefix || 'source'
                ) OR (
                    input.status = 'materializing'
                    AND input.generation = attempt.generation
                    AND input.lease_token = attempt.lease_token
                ) AS current_root,
                input.generation > attempt.generation AS superseded
            FROM public.asset_accepted_video_input_attempts AS attempt
            JOIN public.asset_accepted_video_inputs AS input
              ON input.input_id = attempt.input_id
            WHERE attempt.prefix = v_item.prefix
            UNION ALL
            SELECT input.input_id, input.input_owner,
                (
                    input.plan IS NOT NULL
                    AND input.plan ->> 'plan_digest' = frame.plan_digest
                    AND frame.generation = attempt.generation
                    AND pg_catalog.left(
                        frame.blob #>> '{key}', pg_catalog.char_length(v_item.prefix)
                    ) = v_item.prefix
                ) OR (
                    frame.status = 'materializing'
                    AND frame.generation = attempt.generation
                    AND frame.lease_token = attempt.lease_token
                ) AS current_root,
                frame.generation > attempt.generation AS superseded
            FROM public.asset_accepted_video_frame_attempts AS attempt
            JOIN public.asset_accepted_video_frames AS frame
              ON frame.frame_id = attempt.frame_id
             AND frame.input_id = attempt.input_id
            JOIN public.asset_accepted_video_inputs AS input
              ON input.input_id = attempt.input_id
            WHERE attempt.prefix = v_item.prefix
        ), current_roots AS (
            SELECT DISTINCT input_id, input_owner
            FROM prefix_roots
            WHERE current_root IS TRUE OR superseded IS NOT TRUE
        ), current_tracker_roots AS (
            SELECT DISTINCT input_id
            FROM current_roots
            WHERE input_owner = 'tracker'
        )
        SELECT EXISTS (SELECT 1 FROM prefix_roots),
            (SELECT count(*) FROM current_roots),
            (SELECT count(*) FROM current_tracker_roots),
            (SELECT input_id FROM current_tracker_roots ORDER BY input_id LIMIT 1)
        INTO v_prefix_known, v_current_root_count, v_tracker_input_count,
             v_current_tracker_input_id;

        v_retained := NOT v_prefix_known OR v_current_root_count > 1
            OR v_tracker_input_count > 1;
        IF NOT v_retained AND v_tracker_input_count = 1 THEN
            v_labeling_reply := aap_labeling_v1.video_tracker_input_referenced(
                v_current_tracker_input_id
            );
            IF v_labeling_reply IS NULL
               OR v_labeling_reply ->> 'schema' IS DISTINCT FROM
                    'aap-labeling-video-tracker-inputs/v1'
               OR v_labeling_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
               OR pg_catalog.jsonb_typeof(v_labeling_reply -> 'data') IS DISTINCT FROM 'object'
               OR ((v_labeling_reply -> 'data') - ARRAY[
                    'input_id', 'referenced', 'retained_until'
               ]) IS DISTINCT FROM '{}'::jsonb
               OR NOT (v_labeling_reply -> 'data' ?& ARRAY[
                    'input_id', 'referenced', 'retained_until'
               ])
               OR v_labeling_reply #>> '{data,input_id}' IS DISTINCT FROM
                    v_current_tracker_input_id::text
               OR pg_catalog.jsonb_typeof(
                    v_labeling_reply #> '{data,referenced}'
               ) IS DISTINCT FROM 'boolean'
               OR (v_labeling_reply #> '{data,retained_until}' IS DISTINCT FROM
                    'null'::jsonb
                   AND pg_catalog.jsonb_typeof(
                    v_labeling_reply #> '{data,retained_until}'
                   ) IS DISTINCT FROM 'string') THEN
                v_retained := true;
            ELSE
                v_retained := v_labeling_reply #> '{data,referenced}' = 'true'::jsonb;
            END IF;
        END IF;

        IF v_retained THEN
            -- A held first page cannot starve later safe work: move each
            -- blocker by one existing claim lease, then continue this page.
            UPDATE public.asset_accepted_input_garbage AS garbage
            SET not_before = GREATEST(
                garbage.not_before,
                v_now + pg_catalog.make_interval(secs => p_lease_seconds)
            )
            WHERE garbage.garbage_id = v_item.garbage_id;
            CONTINUE;
        END IF;

        v_expires := v_now + pg_catalog.make_interval(secs => p_lease_seconds);
        v_token := pg_catalog.gen_random_uuid();
        UPDATE public.asset_accepted_input_garbage AS garbage
        SET lease_owner = p_worker_id, lease_token = v_token, lease_expires_at = v_expires
        WHERE garbage.garbage_id = v_item.garbage_id
        RETURNING * INTO v_item;
        v_items := v_items || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'garbage_id', v_item.garbage_id,
                'prefix', v_item.prefix,
                'lease_token', v_token
            )
        );
    END LOOP;
    RETURN aap_assets_v1._video_input_reply(
        pg_catalog.jsonb_build_object('items', v_items), NULL
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'input_busy');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_assets_v1.claim_accepted_video_input_garbage(uuid, integer, integer)
    FROM PUBLIC;
