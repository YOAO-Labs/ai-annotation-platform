-- Keep accepted tracker inputs while a Jobs execution or unknown started legacy
-- writer can still depend on them. Jobs owns the execution proof; Labeling keeps
-- the existing business-status, annotation-pin and review-grace rules.
CREATE FUNCTION aap_jobs_v1.video_tracker_input_retention_facts(
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
    SELECT job.started_at INTO v_job_started_at
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
    ELSIF v_journal_exists THEN
        v_execution_retained := true;
    ELSIF v_job_exists THEN
        v_execution_retained := v_job_started_at IS NOT NULL;
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

CREATE OR REPLACE FUNCTION aap_labeling_v1.video_tracker_input_referenced(
    p_input_id uuid
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
    v_scope public.video_tracker_input_scopes%ROWTYPE;
    v_job public.video_tracker_jobs%ROWTYPE;
    v_job_id uuid;
    v_job_retention jsonb;
    v_retained boolean;
    v_until timestamptz;
    v_referenced boolean := false;
    v_checkpoint_retained boolean;
BEGIN
    IF p_input_id IS NULL THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL, 'invalid_request');
    END IF;
    v_checkpoint_retained := aap_jobs_v1.video_execution_input_retained(p_input_id);
    IF v_checkpoint_retained IS NULL THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL, 'invalid_response');
    ELSIF v_checkpoint_retained IS TRUE THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(
            pg_catalog.jsonb_build_object(
                'input_id', p_input_id, 'referenced', true, 'retained_until', NULL
            ), NULL
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.annotation_video_tracker_input_sources AS pin
        WHERE pin.input_id = p_input_id
    ) THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(
            pg_catalog.jsonb_build_object(
                'input_id', p_input_id, 'referenced', true, 'retained_until', NULL
            ), NULL
        );
    END IF;

    SELECT scope.* INTO v_scope
    FROM public.video_tracker_input_scopes AS scope
    WHERE scope.input_id = p_input_id
    FOR SHARE NOWAIT;
    IF FOUND THEN
        v_job_id := v_scope.tracker_job_id;
    END IF;
    v_job_retention := aap_jobs_v1.video_tracker_input_retention_facts(
        p_input_id, v_job_id
    );
    IF v_job_retention IS NULL
       OR v_job_retention ->> 'schema' IS DISTINCT FROM
            'aap-jobs-video-tracker-input-retention/v1'
       OR v_job_retention -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_job_retention -> 'data') IS DISTINCT FROM 'object'
       OR ((v_job_retention -> 'data') - ARRAY[
            'input_id', 'tracker_job_id', 'execution_retained'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_job_retention -> 'data' ?& ARRAY[
            'input_id', 'tracker_job_id', 'execution_retained'
       ])
       OR v_job_retention #>> '{data,input_id}' IS DISTINCT FROM p_input_id::text
       OR pg_catalog.jsonb_typeof(v_job_retention #> '{data,tracker_job_id}')
            IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_job_retention #> '{data,execution_retained}')
            IS DISTINCT FROM 'boolean'
       OR (v_job_id IS NOT NULL AND v_job_retention #>> '{data,tracker_job_id}'
            IS DISTINCT FROM v_job_id::text) THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL, 'invalid_response');
    END IF;
    BEGIN
        v_job_id := (v_job_retention #>> '{data,tracker_job_id}')::uuid;
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL, 'invalid_response');
    END;
    IF v_job_id IS NULL OR v_job_id::text IS DISTINCT FROM
       v_job_retention #>> '{data,tracker_job_id}' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL, 'invalid_response');
    END IF;
    v_retained := v_job_retention #> '{data,execution_retained}' = 'true'::jsonb;
    IF v_retained THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(
            pg_catalog.jsonb_build_object(
                'input_id', p_input_id, 'referenced', true, 'retained_until', NULL
            ), NULL
        );
    END IF;

    SELECT job.id, job.task_id, job.dataset_item_id, job.annotation_id,
        job.segment_id, job.target_class_name, job.target_tool_unit_id,
        job.created_by, job.status, job.job_kind, job.track_id_snapshot,
        job.correction_frame, job.model_key, job.direction, job.from_frame,
        job.to_frame, job.prompt, job.staged_result, job.revision,
        job.cancel_requested_at, job.started_at, job.created_at, job.completed_at
    INTO v_job.id, v_job.task_id, v_job.dataset_item_id, v_job.annotation_id,
        v_job.segment_id, v_job.target_class_name, v_job.target_tool_unit_id,
        v_job.created_by, v_job.status, v_job.job_kind, v_job.track_id_snapshot,
        v_job.correction_frame, v_job.model_key, v_job.direction, v_job.from_frame,
        v_job.to_frame, v_job.prompt, v_job.staged_result, v_job.revision,
        v_job.cancel_requested_at, v_job.started_at, v_job.created_at, v_job.completed_at
    FROM public.video_tracker_jobs AS job
    WHERE job.id = v_job_id
    FOR SHARE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(
            pg_catalog.jsonb_build_object(
                'input_id', p_input_id, 'referenced', false, 'retained_until', NULL
            ), NULL
        );
    END IF;

    v_until := COALESCE(v_job.completed_at, v_job.created_at) + interval '24 hours';
    IF v_job.status IN ('queued', 'running') THEN
        v_referenced := true;
        v_until := NULL;
    ELSIF v_job.status IN ('pending_review', 'partially_reviewed')
       AND v_job.staged_result IS NOT NULL
       AND v_until > pg_catalog.clock_timestamp() THEN
        v_referenced := true;
    ELSIF v_job.status = 'cancelled' AND v_job.job_kind = 'tracking'
       AND v_job.staged_result IS NOT NULL
       AND v_until > pg_catalog.clock_timestamp() THEN
        v_referenced := true;
    ELSE
        v_until := NULL;
    END IF;
    RETURN aap_labeling_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object(
            'input_id', p_input_id, 'referenced', v_referenced,
            'retained_until', v_until
        ), NULL
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL, 'input_busy');
    WHEN OTHERS THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.video_tracker_input_referenced(uuid) FROM PUBLIC;
