CREATE FUNCTION aap_labeling_v1.task_workflow_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_keys text[];
    v_expected_keys text[];
    v_task_id uuid;
    v_project_id uuid;
    v_batch_id uuid;
    v_actor_id uuid;
    v_expected_status text;
    v_expected_round_id uuid;
    v_observed_project_id uuid;
    v_observed_batch_id uuid;
    v_batch_annotator_id uuid;
    v_task record;
    v_actor_lock jsonb;
    v_actor jsonb;
    v_at timestamptz;
    v_reason text;
    v_reason_type text;
    v_effective_assignee_id uuid;
    v_inherited_assignee_id uuid;
    v_original_reviewer_id uuid;
    v_assigned_at timestamptz;
    v_actor_ids uuid[] := ARRAY[]::uuid[];
    v_value jsonb;
    v_id uuid;
    v_evidence_valid boolean;
    v_contributors jsonb;
    v_changed boolean := false;
    v_reply_data jsonb;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-task-workflow/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Labeling task-workflow request'
            USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS keys(key);
    v_expected_keys := ARRAY[
        'actor_id', 'batch_id', 'expected_review_round_id', 'expected_status',
        'operation', 'project_id', 'schema', 'task_id'
    ]::text[];
    CASE v_operation
        WHEN 'submit', 'complete_video_review', 'reopen', 'claim_review', 'approve_review' THEN
            v_expected_keys := v_expected_keys || ARRAY['at']::text[];
        WHEN 'skip' THEN
            v_expected_keys := v_expected_keys || ARRAY['at', 'reason']::text[];
        WHEN 'reject_review' THEN
            v_expected_keys := v_expected_keys || ARRAY['at', 'reason', 'reason_type']::text[];
        WHEN 'freeze_review_evidence', 'capture_first_review_snapshot' THEN
            v_expected_keys := v_expected_keys || ARRAY['contributor_ids']::text[];
        WHEN 'record_first_review_fact' THEN
            v_expected_keys := v_expected_keys || ARRAY['at', 'contributor_ids', 'result']::text[];
        WHEN 'withdraw', 'reopen_video_segment', 'accept_rejection', 'record_annotation_actor', 'clear_review_evidence' THEN
            NULL;
        ELSE
            RAISE EXCEPTION 'Invalid Labeling task-workflow operation'
                USING ERRCODE = '22023';
    END CASE;
    SELECT array_agg(key ORDER BY key) INTO v_expected_keys
    FROM unnest(v_expected_keys) AS expected(key);
    IF v_keys IS DISTINCT FROM v_expected_keys
       OR jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_request -> 'expected_status') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_request -> 'batch_id') NOT IN ('string', 'null')
       OR jsonb_typeof(p_request -> 'expected_review_round_id') NOT IN ('string', 'null') THEN
        RAISE EXCEPTION 'Invalid Labeling task-workflow fields'
            USING ERRCODE = '22023';
    END IF;

    v_task_id := (p_request ->> 'task_id')::uuid;
    v_project_id := (p_request ->> 'project_id')::uuid;
    v_actor_id := (p_request ->> 'actor_id')::uuid;
    v_batch_id := (p_request ->> 'batch_id')::uuid;
    v_expected_round_id := (p_request ->> 'expected_review_round_id')::uuid;
    v_expected_status := p_request ->> 'expected_status';
    IF v_operation IN ('submit', 'complete_video_review', 'skip', 'reopen', 'claim_review', 'approve_review', 'reject_review', 'record_first_review_fact') THEN
        IF jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string'
           OR p_request ->> 'at' !~ '([Zz]|[+-][0-9]{2}:[0-9]{2})$' THEN
            RAISE EXCEPTION 'Invalid Labeling task-workflow timestamp'
                USING ERRCODE = '22023';
        END IF;
        v_at := (p_request ->> 'at')::timestamptz;
    END IF;
    IF v_operation IN ('skip', 'reject_review') THEN
        IF (v_operation = 'skip' AND jsonb_typeof(p_request -> 'reason') IS DISTINCT FROM 'string')
           OR (v_operation = 'reject_review'
               AND jsonb_typeof(p_request -> 'reason') NOT IN ('string', 'null')) THEN
            RAISE EXCEPTION 'Invalid Labeling task-workflow reason'
                USING ERRCODE = '22023';
        END IF;
        v_reason := p_request ->> 'reason';
    END IF;
    IF v_operation = 'reject_review' THEN
        IF jsonb_typeof(p_request -> 'reason_type') IS DISTINCT FROM 'string'
           OR p_request ->> 'reason_type' NOT IN ('missing', 'extra', 'wrong_label', 'wrong_geometry') THEN
            RAISE EXCEPTION 'Invalid Labeling task-workflow reason type'
                USING ERRCODE = '22023';
        END IF;
        v_reason_type := p_request ->> 'reason_type';
    END IF;
    IF v_operation = 'record_first_review_fact'
       AND (jsonb_typeof(p_request -> 'result') IS DISTINCT FROM 'string'
            OR p_request ->> 'result' NOT IN ('approved', 'rejected')) THEN
        RAISE EXCEPTION 'Invalid Labeling first-review result'
            USING ERRCODE = '22023';
    END IF;
    IF v_operation IN ('freeze_review_evidence', 'capture_first_review_snapshot', 'record_first_review_fact')
       AND jsonb_typeof(p_request -> 'contributor_ids') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'Invalid Labeling contributor evidence'
            USING ERRCODE = '22023';
    END IF;
    IF v_operation IN ('freeze_review_evidence', 'capture_first_review_snapshot', 'record_first_review_fact')
       AND EXISTS (
           SELECT 1 FROM jsonb_array_elements(p_request -> 'contributor_ids') AS item(value)
           WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'string'
       ) THEN
        RAISE EXCEPTION 'Invalid Labeling contributor evidence'
            USING ERRCODE = '22023';
    END IF;

    SELECT task.project_id, task.batch_id
    INTO v_observed_project_id, v_observed_batch_id
    FROM public.tasks AS task
    WHERE task.id = v_task_id;
    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
            'request', p_request,
            'error', jsonb_build_object('code', 'task_not_found')
        );
    END IF;
    IF v_observed_project_id IS DISTINCT FROM v_project_id
       OR v_observed_batch_id IS DISTINCT FROM v_batch_id THEN
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
            'request', p_request,
            'error', jsonb_build_object('code', 'task_target_changed')
        );
    END IF;

    v_actor_lock := aap_identity_v1.lock_accounts_share(ARRAY[v_actor_id]::uuid[], true);
    SELECT account.value INTO v_actor
    FROM jsonb_array_elements(COALESCE(v_actor_lock -> 'accounts', '[]'::jsonb)) AS account(value)
    WHERE account.value ->> 'id' = v_actor_id::text;
    IF v_actor IS NULL OR v_actor ->> 'is_active' IS DISTINCT FROM 'true' THEN
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
            'request', p_request,
            'error', jsonb_build_object('code', 'actor_inactive')
        );
    END IF;

    IF v_batch_id IS NOT NULL THEN
        SELECT batch.annotator_id INTO v_batch_annotator_id
        FROM public.task_batches AS batch
        WHERE batch.id = v_batch_id AND batch.project_id = v_project_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                'request', p_request,
                'error', jsonb_build_object('code', 'task_target_changed')
            );
        END IF;
    END IF;
    SELECT task.id, task.project_id, task.batch_id, task.status,
           task.assignee_id, task.assigned_at, task.submitted_at,
           task.reviewer_id, task.reviewer_is_override, task.reviewer_claimed_at,
           task.reviewed_at, task.reject_reason, task.reject_reason_type,
           task.skip_reason, task.skipped_at, task.reopened_count,
           task.last_reopened_at, task.review_round_id,
           task.annotation_contributor_ids, task.review_contributor_ids,
           task.review_submitter_id, task.first_review_eligible,
           task.first_reviewed_at, task.first_review_result,
           task.first_review_contributor_ids
    INTO v_task
    FROM public.tasks AS task
    WHERE task.id = v_task_id AND task.project_id = v_project_id
      AND task.batch_id IS NOT DISTINCT FROM v_batch_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
            'request', p_request,
            'error', jsonb_build_object('code', 'task_target_changed')
        );
    END IF;
    IF v_task.status IS DISTINCT FROM v_expected_status
       OR v_task.review_round_id IS DISTINCT FROM v_expected_round_id THEN
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
            'request', p_request,
            'error', jsonb_build_object(
                'code', 'task_state_changed',
                'details', jsonb_build_object('status', v_task.status)
            )
        );
    END IF;

    CASE v_operation
        WHEN 'submit' THEN
            IF v_task.status NOT IN ('pending', 'in_progress') THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object('code', 'task_state_changed',
                        'details', jsonb_build_object('reason', 'task_not_submittable', 'status', v_task.status))
                );
            END IF;
            IF v_task.assignee_id IS NULL THEN
                v_effective_assignee_id := COALESCE(v_batch_annotator_id, v_actor_id);
                SELECT lock.created_at INTO v_assigned_at
                FROM public.task_locks AS lock
                WHERE lock.task_id = v_task_id AND lock.user_id = v_effective_assignee_id
                  AND lock.expire_at > v_at AND lock.created_at <= v_at;
                UPDATE public.tasks AS task
                SET assignee_id = v_effective_assignee_id,
                    assigned_at = COALESCE(v_assigned_at, v_at),
                    status = 'review', submitted_at = v_at,
                    reviewer_id = NULL, reviewer_is_override = false,
                    reviewer_claimed_at = NULL, reviewed_at = NULL,
                    reject_reason = NULL, reject_reason_type = NULL,
                    review_round_id = gen_random_uuid(), updated_at = clock_timestamp()
                WHERE task.id = v_task_id;
            ELSE
                UPDATE public.tasks AS task
                SET status = 'review', submitted_at = v_at,
                    reviewer_id = NULL, reviewer_is_override = false,
                    reviewer_claimed_at = NULL, reviewed_at = NULL,
                    reject_reason = NULL, reject_reason_type = NULL,
                    review_round_id = gen_random_uuid(), updated_at = clock_timestamp()
                WHERE task.id = v_task_id;
            END IF;
            v_changed := true;
        WHEN 'complete_video_review' THEN
            UPDATE public.tasks AS task
            SET status = 'review', submitted_at = v_at,
                reviewer_id = NULL, reviewer_is_override = false,
                reviewer_claimed_at = NULL, reviewed_at = NULL,
                review_round_id = CASE
                    WHEN task.status = 'review' THEN task.review_round_id
                    ELSE gen_random_uuid() END,
                updated_at = clock_timestamp()
            WHERE task.id = v_task_id;
            v_changed := true;
        WHEN 'skip' THEN
            IF v_task.status NOT IN ('pending', 'in_progress') THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object('code', 'task_state_changed',
                        'details', jsonb_build_object('reason', 'task_not_skippable', 'status', v_task.status))
                );
            END IF;
            v_inherited_assignee_id := COALESCE(v_task.assignee_id, v_batch_annotator_id);
            IF v_task.assignee_id IS NULL THEN
                SELECT lock.created_at INTO v_assigned_at
                FROM public.task_locks AS lock
                WHERE lock.task_id = v_task_id AND lock.user_id = v_actor_id
                  AND lock.expire_at > v_at AND lock.created_at <= v_at;
                UPDATE public.tasks AS task
                SET assignee_id = v_actor_id,
                    assigned_at = COALESCE(v_assigned_at, v_at),
                    status = 'review', skip_reason = v_reason, skipped_at = v_at,
                    submitted_at = v_at,
                    reviewer_id = NULL, reviewer_is_override = false,
                    reviewer_claimed_at = NULL, reviewed_at = NULL,
                    reject_reason = NULL, reject_reason_type = NULL,
                    review_round_id = gen_random_uuid(), updated_at = clock_timestamp()
                WHERE task.id = v_task_id;
            ELSE
                UPDATE public.tasks AS task
                SET status = 'review', skip_reason = v_reason, skipped_at = v_at,
                    submitted_at = v_at,
                    reviewer_id = NULL, reviewer_is_override = false,
                    reviewer_claimed_at = NULL, reviewed_at = NULL,
                    reject_reason = NULL, reject_reason_type = NULL,
                    review_round_id = gen_random_uuid(), updated_at = clock_timestamp()
                WHERE task.id = v_task_id;
            END IF;
            v_changed := true;
        WHEN 'withdraw' THEN
            IF v_task.status <> 'review' THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object('code', 'task_state_changed',
                        'details', jsonb_build_object('reason', 'task_not_in_review', 'status', v_task.status))
                );
            END IF;
            IF v_task.reviewer_claimed_at IS NOT NULL THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object('code', 'task_already_claimed',
                        'details', jsonb_build_object('reviewer_id', v_task.reviewer_id))
                );
            END IF;
            UPDATE public.tasks AS task
            SET status = 'in_progress', submitted_at = NULL, updated_at = clock_timestamp()
            WHERE task.id = v_task_id;
            v_changed := true;
        WHEN 'reopen' THEN
            IF v_task.status <> 'completed' THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object('code', 'task_state_changed',
                        'details', jsonb_build_object('reason', 'task_not_completed', 'status', v_task.status))
                );
            END IF;
            v_original_reviewer_id := v_task.reviewer_id;
            UPDATE public.tasks AS task
            SET status = 'in_progress', reopened_count = COALESCE(task.reopened_count, 0) + 1,
                last_reopened_at = v_at, reviewer_id = NULL, reviewer_is_override = false,
                reviewer_claimed_at = NULL, reviewed_at = NULL, reject_reason = NULL,
                reject_reason_type = NULL, submitted_at = NULL,
                review_round_id = COALESCE(task.review_round_id, gen_random_uuid()),
                updated_at = clock_timestamp()
            WHERE task.id = v_task_id;
            v_changed := true;
        WHEN 'reopen_video_segment' THEN
            UPDATE public.tasks AS task
            SET status = 'in_progress', submitted_at = NULL, reviewed_at = NULL,
                updated_at = clock_timestamp()
            WHERE task.id = v_task_id;
            v_changed := true;
        WHEN 'accept_rejection' THEN
            IF v_task.status <> 'rejected' THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object('code', 'task_state_changed',
                        'details', jsonb_build_object('reason', 'task_not_rejected', 'status', v_task.status))
                );
            END IF;
            UPDATE public.tasks AS task
            SET status = 'in_progress', review_round_id = COALESCE(task.review_round_id, gen_random_uuid()),
                updated_at = clock_timestamp()
            WHERE task.id = v_task_id;
            v_changed := true;
        WHEN 'claim_review' THEN
            IF v_task.status <> 'review' THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object('code', 'task_state_changed',
                        'details', jsonb_build_object('reason', 'task_not_in_review', 'status', v_task.status))
                );
            END IF;
            IF v_task.reviewer_claimed_at IS NOT NULL THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object('code', 'task_already_claimed',
                        'details', jsonb_build_object('reviewer_id', v_task.reviewer_id))
                );
            END IF;
            UPDATE public.tasks AS task
            SET reviewer_id = v_actor_id, reviewer_claimed_at = v_at,
                review_round_id = COALESCE(task.review_round_id, gen_random_uuid()),
                updated_at = clock_timestamp()
            WHERE task.id = v_task_id;
            v_changed := true;
        WHEN 'approve_review' THEN
            IF v_task.status <> 'review' THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object('code', 'task_state_changed',
                        'details', jsonb_build_object('reason', 'task_not_in_review', 'status', v_task.status))
                );
            END IF;
            UPDATE public.tasks AS task
            SET status = 'completed', reviewed_at = v_at,
                reviewer_id = COALESCE(task.reviewer_id, v_actor_id),
                reviewer_claimed_at = COALESCE(task.reviewer_claimed_at, v_at),
                review_round_id = COALESCE(task.review_round_id, gen_random_uuid()),
                updated_at = clock_timestamp()
            WHERE task.id = v_task_id;
            v_changed := true;
        WHEN 'reject_review' THEN
            IF v_task.status <> 'review' THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object('code', 'task_state_changed',
                        'details', jsonb_build_object('reason', 'task_not_in_review', 'status', v_task.status))
                );
            END IF;
            UPDATE public.tasks AS task
            SET status = 'rejected', reviewed_at = v_at,
                reject_reason_type = v_reason_type, reject_reason = v_reason,
                reviewer_id = COALESCE(task.reviewer_id, v_actor_id),
                reviewer_claimed_at = COALESCE(task.reviewer_claimed_at, v_at),
                review_round_id = COALESCE(task.review_round_id, gen_random_uuid()),
                updated_at = clock_timestamp()
            WHERE task.id = v_task_id;
            v_changed := true;
        WHEN 'record_annotation_actor' THEN
            IF v_task.status <> 'review' THEN
                v_evidence_valid := jsonb_typeof(v_task.annotation_contributor_ids) = 'array';
                IF v_evidence_valid THEN
                    FOR v_value IN SELECT value FROM jsonb_array_elements(v_task.annotation_contributor_ids)
                    LOOP
                        IF jsonb_typeof(v_value) IS DISTINCT FROM 'string' THEN
                            v_evidence_valid := false;
                            EXIT;
                        END IF;
                        BEGIN
                            v_id := (v_value #>> '{}')::uuid;
                        EXCEPTION WHEN invalid_text_representation THEN
                            v_evidence_valid := false;
                            EXIT;
                        END;
                        v_actor_ids := array_append(v_actor_ids, v_id);
                    END LOOP;
                END IF;
                IF v_evidence_valid AND NOT (v_actor_id = ANY(v_actor_ids)) THEN
                    v_actor_ids := array_append(v_actor_ids, v_actor_id);
                    SELECT array_agg(DISTINCT actor_id ORDER BY actor_id)
                    INTO v_actor_ids FROM unnest(v_actor_ids) AS actors(actor_id);
                    SELECT jsonb_agg(to_jsonb(actor_id::text) ORDER BY actor_id::text)
                    INTO v_contributors FROM unnest(v_actor_ids) AS actors(actor_id);
                    UPDATE public.tasks AS task
                    SET annotation_contributor_ids = v_contributors, updated_at = clock_timestamp()
                    WHERE task.id = v_task_id;
                    v_changed := true;
                END IF;
            END IF;
        WHEN 'freeze_review_evidence' THEN
            IF v_task.status <> 'review' THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object('code', 'task_state_changed',
                        'details', jsonb_build_object('reason', 'task_not_in_review', 'status', v_task.status))
                );
            END IF;
            v_evidence_valid := jsonb_typeof(v_task.annotation_contributor_ids) = 'array';
            IF v_evidence_valid THEN
                FOR v_value IN SELECT value FROM jsonb_array_elements(v_task.annotation_contributor_ids)
                LOOP
                    IF jsonb_typeof(v_value) IS DISTINCT FROM 'string' THEN
                        v_evidence_valid := false;
                        EXIT;
                    END IF;
                    BEGIN
                        v_id := (v_value #>> '{}')::uuid;
                    EXCEPTION WHEN invalid_text_representation THEN
                        v_evidence_valid := false;
                        EXIT;
                    END;
                    v_actor_ids := array_append(v_actor_ids, v_id);
                END LOOP;
            END IF;
            FOR v_value IN SELECT value FROM jsonb_array_elements(p_request -> 'contributor_ids')
            LOOP
                IF jsonb_typeof(v_value) IS DISTINCT FROM 'string' THEN
                    v_evidence_valid := false;
                    CONTINUE;
                END IF;
                BEGIN
                    v_id := (v_value #>> '{}')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    v_evidence_valid := false;
                    CONTINUE;
                END;
                IF v_evidence_valid THEN
                    v_actor_ids := array_append(v_actor_ids, v_id);
                END IF;
            END LOOP;
            IF v_evidence_valid THEN
                v_actor_ids := array_append(v_actor_ids, v_actor_id);
                SELECT array_agg(DISTINCT actor_id ORDER BY actor_id)
                INTO v_actor_ids FROM unnest(v_actor_ids) AS actors(actor_id);
                SELECT jsonb_agg(to_jsonb(actor_id::text) ORDER BY actor_id::text)
                INTO v_contributors FROM unnest(v_actor_ids) AS actors(actor_id);
            ELSE
                v_contributors := 'null'::jsonb;
            END IF;
            UPDATE public.tasks AS task
            SET review_submitter_id = v_actor_id, review_contributor_ids = v_contributors,
                updated_at = clock_timestamp()
            WHERE task.id = v_task_id;
            v_changed := true;
        WHEN 'clear_review_evidence' THEN
            UPDATE public.tasks AS task
            SET review_contributor_ids = CASE
                    WHEN task.review_contributor_ids IS NULL THEN NULL
                    ELSE 'null'::jsonb END,
                review_submitter_id = NULL,
                updated_at = clock_timestamp()
            WHERE task.id = v_task_id
              AND ((task.review_contributor_ids IS NOT NULL
                    AND task.review_contributor_ids <> 'null'::jsonb)
                   OR task.review_submitter_id IS NOT NULL);
            v_changed := FOUND;
        WHEN 'capture_first_review_snapshot' THEN
            IF v_task.status <> 'review' THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object('code', 'task_state_changed',
                        'details', jsonb_build_object('reason', 'task_not_in_review', 'status', v_task.status))
                );
            END IF;
            SELECT COALESCE(jsonb_agg(to_jsonb(actor) ORDER BY actor), '[]'::jsonb)
            INTO v_contributors
            FROM (SELECT DISTINCT value #>> '{}' AS actor
                  FROM jsonb_array_elements(p_request -> 'contributor_ids')) AS contributors;
            UPDATE public.tasks AS task
            SET first_review_contributor_ids = v_contributors,
                updated_at = clock_timestamp()
            WHERE task.id = v_task_id
              AND task.first_review_eligible IS TRUE AND task.first_reviewed_at IS NULL
              AND task.first_review_contributor_ids IS DISTINCT FROM v_contributors;
            v_changed := FOUND;
        WHEN 'record_first_review_fact' THEN
            IF (p_request ->> 'result' = 'approved' AND v_task.status <> 'completed')
               OR (p_request ->> 'result' = 'rejected' AND v_task.status <> 'rejected') THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-workflow/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object('code', 'task_state_changed',
                        'details', jsonb_build_object('reason', 'review_result_changed', 'status', v_task.status))
                );
            END IF;
            SELECT COALESCE(jsonb_agg(to_jsonb(actor) ORDER BY actor), '[]'::jsonb)
            INTO v_contributors
            FROM (SELECT DISTINCT value #>> '{}' AS actor
                  FROM jsonb_array_elements(p_request -> 'contributor_ids')) AS contributors;
            UPDATE public.tasks AS task
            SET first_reviewed_at = v_at, first_review_result = p_request ->> 'result',
                first_review_contributor_ids = v_contributors,
                updated_at = clock_timestamp()
            WHERE task.id = v_task_id
              AND task.first_review_eligible IS TRUE AND task.first_reviewed_at IS NULL;
            v_changed := FOUND;
    END CASE;

    SELECT task.id, task.project_id, task.batch_id, task.status,
           task.assignee_id, task.assigned_at, task.submitted_at,
           task.reviewer_id, task.reviewer_is_override, task.reviewer_claimed_at,
           task.reviewed_at, task.reject_reason, task.reject_reason_type,
           task.skip_reason, task.skipped_at, task.reopened_count,
           task.last_reopened_at, task.review_round_id,
           task.annotation_contributor_ids, task.review_contributor_ids,
           task.review_submitter_id, task.first_review_eligible,
           task.first_reviewed_at, task.first_review_result,
           task.first_review_contributor_ids
    INTO v_task
    FROM public.tasks AS task WHERE task.id = v_task_id;
    v_reply_data := jsonb_build_object(
        'task', jsonb_build_object(
            'id', v_task.id, 'project_id', v_task.project_id, 'batch_id', v_task.batch_id,
            'status', v_task.status, 'assignee_id', v_task.assignee_id,
            'assigned_at', v_task.assigned_at, 'submitted_at', v_task.submitted_at,
            'reviewer_id', v_task.reviewer_id, 'reviewer_is_override', v_task.reviewer_is_override,
            'reviewer_claimed_at', v_task.reviewer_claimed_at, 'reviewed_at', v_task.reviewed_at,
            'reject_reason', v_task.reject_reason, 'reject_reason_type', v_task.reject_reason_type,
            'skip_reason', v_task.skip_reason, 'skipped_at', v_task.skipped_at,
            'reopened_count', v_task.reopened_count, 'last_reopened_at', v_task.last_reopened_at,
            'review_round_id', v_task.review_round_id,
            'annotation_contributor_ids', v_task.annotation_contributor_ids,
            'review_contributor_ids', v_task.review_contributor_ids,
            'review_submitter_id', v_task.review_submitter_id,
            'first_review_eligible', v_task.first_review_eligible,
            'first_reviewed_at', v_task.first_reviewed_at,
            'first_review_result', v_task.first_review_result,
            'first_review_contributor_ids', v_task.first_review_contributor_ids
        ),
        'changed', v_changed,
        'inherited_assignee_id', v_inherited_assignee_id,
        'original_reviewer_id', v_original_reviewer_id
    );
    RETURN jsonb_build_object(
        'schema', 'aap-labeling-task-workflow/v1', 'ok', true,
        'request', p_request, 'data', v_reply_data
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.task_workflow_command(jsonb) FROM PUBLIC;
