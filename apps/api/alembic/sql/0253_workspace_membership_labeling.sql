-- Labeling-owned task, batch and task-lock facts and effects for Workspace
-- project-member role changes. Workspace retains the surrounding transaction.
CREATE FUNCTION aap_labeling_v1.read_membership_work(
    p_project_id uuid,
    p_user_id uuid,
    p_annotation_receiver_id uuid,
    p_review_receiver_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_unfinished_annotation CONSTANT text[] :=
        ARRAY['pending', 'in_progress', 'rejected']::text[];
    v_unfinished_review CONSTANT text[] := ARRAY['review']::text[];
    v_unfinished_work CONSTANT text[] :=
        ARRAY['pending', 'in_progress', 'rejected', 'review']::text[];
    v_annotation_task_ids uuid[] := ARRAY[]::uuid[];
    v_review_task_ids uuid[] := ARRAY[]::uuid[];
    v_unassigned_review_task_ids uuid[] := ARRAY[]::uuid[];
    v_lock_task_ids uuid[] := ARRAY[]::uuid[];
    v_review_claim_task_ids uuid[] := ARRAY[]::uuid[];
    v_batch_annotator_ids uuid[] := ARRAY[]::uuid[];
    v_batch_reviewer_ids uuid[] := ARRAY[]::uuid[];
    v_materialization_annotation_task_ids uuid[] := ARRAY[]::uuid[];
    v_materialization_review_task_ids uuid[] := ARRAY[]::uuid[];
    v_affected_task_ids uuid[] := ARRAY[]::uuid[];
    v_batch_ids uuid[] := ARRAY[]::uuid[];
    v_annotation_receiver_conflict_task_ids uuid[] := ARRAY[]::uuid[];
    v_review_receiver_unknown_task_ids uuid[] := ARRAY[]::uuid[];
    v_review_receiver_annotator_task_ids uuid[] := ARRAY[]::uuid[];
    v_review_receiver_contributor_task_ids uuid[] := ARRAY[]::uuid[];
    v_tasks jsonb := '[]'::jsonb;
    v_batches jsonb := '[]'::jsonb;
BEGIN
    IF p_project_id IS NULL OR p_user_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
    )
    INTO v_annotation_task_ids
    FROM public.tasks AS task
    LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
    WHERE task.project_id = p_project_id
      AND task.status = ANY(v_unfinished_annotation)
      AND COALESCE(task.assignee_id, batch.annotator_id) = p_user_id;

    SELECT COALESCE(
        pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
    )
    INTO v_review_task_ids
    FROM public.tasks AS task
    LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
    WHERE task.project_id = p_project_id
      AND task.status = ANY(v_unfinished_review)
      AND COALESCE(task.reviewer_id, batch.reviewer_id) = p_user_id;

    SELECT COALESCE(
        pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
    )
    INTO v_unassigned_review_task_ids
    FROM public.tasks AS task
    LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
    WHERE task.project_id = p_project_id
      AND task.status = ANY(v_unfinished_review)
      AND COALESCE(task.reviewer_id, batch.reviewer_id) IS NULL;

    SELECT COALESCE(
        pg_catalog.array_agg(task_lock.task_id ORDER BY task_lock.task_id),
        ARRAY[]::uuid[]
    )
    INTO v_lock_task_ids
    FROM public.task_locks AS task_lock
    JOIN public.tasks AS task ON task.id = task_lock.task_id
    WHERE task.project_id = p_project_id
      AND task_lock.user_id = p_user_id
      AND task_lock.expire_at > pg_catalog.statement_timestamp();

    SELECT COALESCE(
        pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
    )
    INTO v_review_claim_task_ids
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
      AND task.status = ANY(v_unfinished_review)
      AND task.reviewer_id = p_user_id
      AND task.reviewer_claimed_at IS NOT NULL;

    SELECT COALESCE(
        pg_catalog.array_agg(batch.id ORDER BY batch.id), ARRAY[]::uuid[]
    )
    INTO v_batch_annotator_ids
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
      AND batch.annotator_id = p_user_id
      AND EXISTS (
          SELECT 1
          FROM public.tasks AS task
          WHERE task.project_id = p_project_id
            AND task.batch_id = batch.id
            AND task.status = ANY(v_unfinished_annotation)
            AND (
                task.assignee_id IS NULL
                OR (
                    task.assignee_id = p_user_id
                    AND task.assignee_is_override IS FALSE
                )
            )
      );

    SELECT COALESCE(
        pg_catalog.array_agg(batch.id ORDER BY batch.id), ARRAY[]::uuid[]
    )
    INTO v_batch_reviewer_ids
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
      AND batch.reviewer_id = p_user_id
      AND EXISTS (
          SELECT 1
          FROM public.tasks AS task
          WHERE task.project_id = p_project_id
            AND task.batch_id = batch.id
            AND task.status = ANY(v_unfinished_work)
            AND (
                task.reviewer_id IS NULL
                OR (
                    task.reviewer_id = p_user_id
                    AND task.reviewer_is_override IS FALSE
                )
            )
      );

    SELECT COALESCE(
        pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
    )
    INTO v_materialization_annotation_task_ids
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
      AND task.batch_id = ANY(v_batch_annotator_ids)
      AND task.assignee_id IS NULL
      AND task.status <> ALL(v_unfinished_annotation);

    SELECT COALESCE(
        pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
    )
    INTO v_materialization_review_task_ids
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
      AND task.batch_id = ANY(v_batch_reviewer_ids)
      AND task.reviewer_id IS NULL
      AND task.status = 'completed';

    SELECT COALESCE(
        pg_catalog.array_agg(DISTINCT requested.task_id ORDER BY requested.task_id),
        ARRAY[]::uuid[]
    )
    INTO v_affected_task_ids
    FROM pg_catalog.unnest(
        v_annotation_task_ids
        || v_review_task_ids
        || v_unassigned_review_task_ids
        || v_lock_task_ids
        || v_review_claim_task_ids
        || v_materialization_annotation_task_ids
        || v_materialization_review_task_ids
    ) AS requested(task_id);

    SELECT COALESCE(
        pg_catalog.array_agg(DISTINCT requested.batch_id ORDER BY requested.batch_id),
        ARRAY[]::uuid[]
    )
    INTO v_batch_ids
    FROM pg_catalog.unnest(v_batch_annotator_ids || v_batch_reviewer_ids)
        AS requested(batch_id);

    IF p_annotation_receiver_id IS NOT NULL THEN
        SELECT COALESCE(
            pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
        )
        INTO v_annotation_receiver_conflict_task_ids
        FROM public.tasks AS task
        LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
        WHERE task.project_id = p_project_id
          AND task.status = ANY(v_unfinished_annotation)
          AND COALESCE(task.assignee_id, batch.annotator_id) = p_user_id
          AND COALESCE(task.reviewer_id, batch.reviewer_id)
                = p_annotation_receiver_id;
    END IF;

    IF p_review_receiver_id IS NOT NULL THEN
        SELECT COALESCE(
            pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
        )
        INTO v_review_receiver_unknown_task_ids
        FROM public.tasks AS task
        LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
        WHERE task.project_id = p_project_id
          AND task.status = ANY(v_unfinished_work)
          AND COALESCE(task.reviewer_id, batch.reviewer_id) = p_user_id
          AND task.review_submitter_id IS DISTINCT FROM p_review_receiver_id
          AND COALESCE(task.annotation_contributor_ids, 'null'::jsonb)
                = 'null'::jsonb;

        SELECT COALESCE(
            pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
        )
        INTO v_review_receiver_annotator_task_ids
        FROM public.tasks AS task
        LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
        WHERE task.project_id = p_project_id
          AND task.status = ANY(v_unfinished_work)
          AND COALESCE(task.reviewer_id, batch.reviewer_id) = p_user_id
          AND COALESCE(task.assignee_id, batch.annotator_id)
                = p_review_receiver_id;

        SELECT COALESCE(
            pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
        )
        INTO v_review_receiver_contributor_task_ids
        FROM public.tasks AS task
        LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
        WHERE task.project_id = p_project_id
          AND task.status = ANY(v_unfinished_work)
          AND COALESCE(task.reviewer_id, batch.reviewer_id) = p_user_id
          AND (
              task.review_submitter_id = p_review_receiver_id
              OR (
                  COALESCE(task.annotation_contributor_ids, 'null'::jsonb)
                        <> 'null'::jsonb
                  AND (
                      pg_catalog.jsonb_exists(
                          task.annotation_contributor_ids,
                          p_review_receiver_id::text
                      )
                      OR (
                          COALESCE(task.review_contributor_ids, 'null'::jsonb)
                                <> 'null'::jsonb
                          AND pg_catalog.jsonb_exists(
                              task.review_contributor_ids,
                              p_review_receiver_id::text
                          )
                      )
                  )
              )
          );
    END IF;

    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'id', task.id,
                'status', task.status,
                'version', task.version,
                'batch_id', task.batch_id,
                'assignee_id', task.assignee_id,
                'assignee_is_override', task.assignee_is_override,
                'reviewer_id', task.reviewer_id,
                'reviewer_is_override', task.reviewer_is_override,
                'reviewer_claimed', task.reviewer_claimed_at IS NOT NULL,
                'review_round_id', task.review_round_id,
                'review_submitter_id', task.review_submitter_id,
                'annotation_contributor_ids', task.annotation_contributor_ids,
                'review_contributor_ids', task.review_contributor_ids,
                'batch_annotator_id', batch.annotator_id,
                'batch_reviewer_id', batch.reviewer_id
            ) ORDER BY task.id
        ),
        '[]'::jsonb
    )
    INTO v_tasks
    FROM public.tasks AS task
    LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
    WHERE task.project_id = p_project_id
      AND task.id = ANY(v_affected_task_ids);

    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'id', batch.id,
                'status', batch.status,
                'annotator_id', batch.annotator_id,
                'reviewer_id', batch.reviewer_id,
                'assigned_user_ids', batch.assigned_user_ids,
                'admin_locked', batch.admin_locked
            ) ORDER BY batch.id
        ),
        '[]'::jsonb
    )
    INTO v_batches
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
      AND batch.id = ANY(v_batch_ids);

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-membership-work/v1',
        'project_id', p_project_id,
        'user_id', p_user_id,
        'annotation_task_ids', v_annotation_task_ids,
        'review_task_ids', v_review_task_ids,
        'unassigned_review_task_ids', v_unassigned_review_task_ids,
        'lock_task_ids', v_lock_task_ids,
        'review_claim_task_ids', v_review_claim_task_ids,
        'materialization_annotation_task_ids', v_materialization_annotation_task_ids,
        'materialization_review_task_ids', v_materialization_review_task_ids,
        'batch_annotator_ids', v_batch_annotator_ids,
        'batch_reviewer_ids', v_batch_reviewer_ids,
        'tasks', v_tasks,
        'batches', v_batches,
        'annotation_receiver_conflict_task_ids', v_annotation_receiver_conflict_task_ids,
        'review_receiver_unknown_task_ids', v_review_receiver_unknown_task_ids,
        'review_receiver_annotator_task_ids', v_review_receiver_annotator_task_ids,
        'review_receiver_contributor_task_ids', v_review_receiver_contributor_task_ids
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_membership_work(uuid, uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

-- Acquire only the sorted resources selected by the old project-member workflow.
-- The caller rolls back the whole transaction after any typed contention result.
CREATE FUNCTION aap_labeling_v1.lock_membership_work(
    p_project_id uuid,
    p_user_id uuid,
    p_target_role text,
    p_is_removal boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_work jsonb;
    v_batch_ids uuid[] := ARRAY[]::uuid[];
    v_annotation_task_ids uuid[] := ARRAY[]::uuid[];
    v_review_task_ids uuid[] := ARRAY[]::uuid[];
    v_lock_task_ids uuid[] := ARRAY[]::uuid[];
    v_materialization_annotation_task_ids uuid[] := ARRAY[]::uuid[];
    v_materialization_review_task_ids uuid[] := ARRAY[]::uuid[];
    v_task_ids uuid[] := ARRAY[]::uuid[];
    v_advisory_task_ids uuid[] := ARRAY[]::uuid[];
    v_task_id uuid;
BEGIN
    IF p_project_id IS NULL
       OR p_user_id IS NULL
       OR p_target_role IS NULL
       OR p_target_role NOT IN ('annotator', 'reviewer', 'viewer')
       OR p_is_removal IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    v_work := aap_labeling_v1.read_membership_work(
        p_project_id, p_user_id, NULL, NULL
    );
    IF v_work ->> 'schema' IS DISTINCT FROM 'aap-labeling-membership-work/v1' THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid membership work facts';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(ids.id ORDER BY ids.id), ARRAY[]::uuid[]
    )
    INTO v_batch_ids
    FROM (
        SELECT entry.value::uuid AS id
        FROM pg_catalog.jsonb_array_elements_text(
            v_work -> 'batch_annotator_ids'
        ) AS entry(value)
        UNION
        SELECT entry.value::uuid AS id
        FROM pg_catalog.jsonb_array_elements_text(
            v_work -> 'batch_reviewer_ids'
        ) AS entry(value)
    ) AS ids;

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_annotation_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'annotation_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_review_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'review_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_lock_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'lock_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_materialization_annotation_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'materialization_annotation_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_materialization_review_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'materialization_review_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(DISTINCT ids.id ORDER BY ids.id), ARRAY[]::uuid[]
    )
    INTO v_advisory_task_ids
    FROM pg_catalog.unnest(
        v_annotation_task_ids || v_review_task_ids || v_lock_task_ids
    ) AS ids(id);

    v_task_ids := v_advisory_task_ids;
    IF NOT p_is_removal THEN
        IF p_target_role <> 'annotator' THEN
            v_task_ids := v_task_ids || v_materialization_annotation_task_ids;
        END IF;
        IF p_target_role <> 'reviewer' THEN
            v_task_ids := v_task_ids || v_materialization_review_task_ids;
        END IF;
    END IF;

    BEGIN
        PERFORM batch.id
        FROM public.task_batches AS batch
        WHERE batch.project_id = p_project_id
          AND batch.id = ANY(v_batch_ids)
        ORDER BY batch.id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR deadlock_detected OR serialization_failure THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'reason', 'resource_busy',
                    'detail', '批次资源正在变更，请刷新后重试'
                )
            );
    END;

    BEGIN
        PERFORM task.id
        FROM public.tasks AS task
        WHERE task.project_id = p_project_id
          AND task.id = ANY(v_task_ids)
        ORDER BY task.id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR deadlock_detected OR serialization_failure THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'reason', 'resource_busy',
                    'detail', '任务资源正在变更，请刷新后重试'
                )
            );
    END;

    FOREACH v_task_id IN ARRAY v_advisory_task_ids LOOP
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'aap:task-edit-lock:' || v_task_id::text,
                0
            )
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'reason', 'resource_busy',
                    'detail', '任务正在编辑，请刷新后重试'
                )
            );
        END IF;
    END LOOP;

    BEGIN
        PERFORM task_lock.id
        FROM public.task_locks AS task_lock
        JOIN public.tasks AS task ON task.id = task_lock.task_id
        WHERE task.project_id = p_project_id
          AND task_lock.user_id = p_user_id
          AND task_lock.expire_at > pg_catalog.clock_timestamp()
        ORDER BY task_lock.task_id, task_lock.user_id
        FOR UPDATE OF task_lock NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR deadlock_detected OR serialization_failure THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'reason', 'resource_busy',
                    'detail', '任务锁正在变更，请刷新后重试'
                )
            );
    END;

    RETURN pg_catalog.jsonb_build_object('ok', true);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_membership_work(uuid, uuid, text, boolean)
    FROM PUBLIC;
-- aap-statement-break

-- Mirror Python's BatchService._replace_assigned_user_id for legacy JSON values.
-- Batch assignment arrays are normally UUID strings; legacy iterable values are
-- still stringified with Python-style scalar and simple container representations.
CREATE FUNCTION aap_labeling_v1.handoff_membership_annotation(
    p_project_id uuid,
    p_from_user_id uuid,
    p_to_user_id uuid
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_unfinished_annotation CONSTANT text[] :=
        ARRAY['pending', 'in_progress', 'rejected']::text[];
    v_batch_ids uuid[] := ARRAY[]::uuid[];
    v_batch record;
    v_iterable jsonb;
    v_item jsonb;
    v_string text;
    v_updated jsonb;
    v_replaced boolean;
BEGIN
    IF p_project_id IS NULL OR p_from_user_id IS NULL OR p_to_user_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(batch.id ORDER BY batch.id), ARRAY[]::uuid[]
    )
    INTO v_batch_ids
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
      AND batch.annotator_id = p_from_user_id
      AND EXISTS (
          SELECT 1
          FROM public.tasks AS task
          WHERE task.project_id = p_project_id
            AND task.batch_id = batch.id
            AND task.status = ANY(v_unfinished_annotation)
            AND (
                task.assignee_id IS NULL
                OR (
                    task.assignee_id = p_from_user_id
                    AND task.assignee_is_override IS FALSE
                )
            )
      );

    UPDATE public.tasks AS task
    SET assignee_id = p_to_user_id,
        updated_at = pg_catalog.now()
    WHERE task.project_id = p_project_id
      AND task.status = ANY(v_unfinished_annotation)
      AND task.id IN (
          SELECT candidate.id
          FROM public.tasks AS candidate
          LEFT JOIN public.task_batches AS batch ON batch.id = candidate.batch_id
          WHERE candidate.project_id = p_project_id
            AND candidate.status = ANY(v_unfinished_annotation)
            AND COALESCE(candidate.assignee_id, batch.annotator_id)
                = p_from_user_id
      );

    -- Freeze historical NULL inheritance before changing the batch default.
    UPDATE public.tasks AS task
    SET assignee_id = p_from_user_id,
        updated_at = pg_catalog.now()
    WHERE task.project_id = p_project_id
      AND task.batch_id = ANY(v_batch_ids)
      AND task.assignee_id IS NULL
      AND task.status <> ALL(v_unfinished_annotation);

    FOR v_batch IN
        SELECT batch.id, batch.assigned_user_ids
        FROM public.task_batches AS batch
        WHERE batch.project_id = p_project_id
          AND batch.id = ANY(v_batch_ids)
        ORDER BY batch.id
    LOOP
        IF v_batch.assigned_user_ids IS NULL
           OR v_batch.assigned_user_ids = 'null'::jsonb THEN
            v_iterable := '[]'::jsonb;
        ELSIF pg_catalog.jsonb_typeof(v_batch.assigned_user_ids) = 'array' THEN
            v_iterable := v_batch.assigned_user_ids;
        ELSIF pg_catalog.jsonb_typeof(v_batch.assigned_user_ids) = 'object' THEN
            SELECT COALESCE(
                pg_catalog.jsonb_agg(pg_catalog.to_jsonb(entry.key)
                    ORDER BY pg_catalog.char_length(entry.key), entry.key COLLATE "C"),
                '[]'::jsonb
            )
            INTO v_iterable
            FROM pg_catalog.jsonb_object_keys(v_batch.assigned_user_ids)
                AS entry(key);
        ELSIF pg_catalog.jsonb_typeof(v_batch.assigned_user_ids) = 'string' THEN
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.to_jsonb(pg_catalog.substr(
                        source_text.assigned_text, positions.character_number, 1
                    )) ORDER BY positions.character_number
                ),
                '[]'::jsonb
            )
            INTO v_iterable
            FROM pg_catalog.generate_series(
                1,
                pg_catalog.char_length(v_batch.assigned_user_ids #>> '{}')
            ) AS positions(character_number)
            CROSS JOIN LATERAL (
                SELECT v_batch.assigned_user_ids #>> '{}' AS assigned_text
            ) AS source_text;
        ELSE
            RAISE EXCEPTION 'legacy assigned_user_ids is not iterable';
        END IF;

        v_updated := '[]'::jsonb;
        v_replaced := false;
        FOR v_item IN
            SELECT element.value
            FROM pg_catalog.jsonb_array_elements(v_iterable) AS element(value)
        LOOP
            CASE pg_catalog.jsonb_typeof(v_item)
                WHEN 'string' THEN v_string := v_item #>> '{}';
                WHEN 'null' THEN v_string := 'None';
                WHEN 'boolean' THEN
                    v_string := CASE WHEN v_item = 'true'::jsonb
                        THEN 'True' ELSE 'False' END;
                WHEN 'number' THEN v_string := v_item::text;
                WHEN 'object' THEN
                    v_string := pg_catalog.replace(v_item::text, '"', '''');
                    v_string := pg_catalog.replace(v_string, ': true', ': True');
                    v_string := pg_catalog.replace(v_string, ': false', ': False');
                    v_string := pg_catalog.replace(v_string, ': null', ': None');
                    v_string := pg_catalog.replace(v_string, ', true', ', True');
                    v_string := pg_catalog.replace(v_string, ', false', ', False');
                    v_string := pg_catalog.replace(v_string, ', null', ', None');
                WHEN 'array' THEN
                    v_string := pg_catalog.replace(v_item::text, '"', '''');
                    v_string := pg_catalog.replace(v_string, 'true', 'True');
                    v_string := pg_catalog.replace(v_string, 'false', 'False');
                    v_string := pg_catalog.replace(v_string, 'null', 'None');
                ELSE
                    RAISE EXCEPTION 'unsupported assigned_user_ids value';
            END CASE;

            IF v_string = p_from_user_id::text THEN
                v_string := p_to_user_id::text;
                v_replaced := true;
            END IF;
            v_updated := v_updated || pg_catalog.jsonb_build_array(v_string);
        END LOOP;

        IF NOT v_replaced THEN
            v_updated := v_updated || pg_catalog.jsonb_build_array(p_to_user_id::text);
        END IF;

        UPDATE public.task_batches AS batch
        SET annotator_id = p_to_user_id,
            assigned_user_ids = v_updated,
            updated_at = pg_catalog.now()
        WHERE batch.id = v_batch.id
          AND batch.project_id = p_project_id;
    END LOOP;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.handoff_membership_annotation(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.handoff_membership_reviewer(
    p_project_id uuid,
    p_from_user_id uuid,
    p_to_user_id uuid
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_unfinished_annotation CONSTANT text[] :=
        ARRAY['pending', 'in_progress', 'rejected']::text[];
    v_unfinished_work CONSTANT text[] :=
        ARRAY['pending', 'in_progress', 'rejected', 'review']::text[];
    v_batch_ids uuid[] := ARRAY[]::uuid[];
    v_batch record;
    v_iterable jsonb;
    v_item jsonb;
    v_string text;
    v_updated jsonb;
    v_replaced boolean;
BEGIN
    IF p_project_id IS NULL OR p_from_user_id IS NULL OR p_to_user_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(batch.id ORDER BY batch.id), ARRAY[]::uuid[]
    )
    INTO v_batch_ids
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
      AND batch.reviewer_id = p_from_user_id
      AND EXISTS (
          SELECT 1
          FROM public.tasks AS task
          WHERE task.project_id = p_project_id
            AND task.batch_id = batch.id
            AND task.status = ANY(v_unfinished_work)
            AND (
                task.reviewer_id IS NULL
                OR (
                    task.reviewer_id = p_from_user_id
                    AND task.reviewer_is_override IS FALSE
                )
            )
      );

    UPDATE public.tasks AS task
    SET reviewer_id = p_to_user_id,
        reviewer_claimed_at = NULL,
        updated_at = pg_catalog.now()
    WHERE task.project_id = p_project_id
      AND task.status = 'review'
      AND task.id IN (
          SELECT candidate.id
          FROM public.tasks AS candidate
          LEFT JOIN public.task_batches AS batch ON batch.id = candidate.batch_id
          WHERE candidate.project_id = p_project_id
            AND candidate.status = 'review'
            AND COALESCE(candidate.reviewer_id, batch.reviewer_id)
                = p_from_user_id
      );

    -- Preserve the old reviewer on completed, NULL-inheriting task history.
    UPDATE public.tasks AS task
    SET reviewer_id = p_from_user_id,
        updated_at = pg_catalog.now()
    WHERE task.project_id = p_project_id
      AND task.batch_id = ANY(v_batch_ids)
      AND task.reviewer_id IS NULL
      AND task.status = 'completed';

    FOR v_batch IN
        SELECT batch.id, batch.assigned_user_ids
        FROM public.task_batches AS batch
        WHERE batch.project_id = p_project_id
          AND batch.id = ANY(v_batch_ids)
        ORDER BY batch.id
    LOOP
        IF v_batch.assigned_user_ids IS NULL
           OR v_batch.assigned_user_ids = 'null'::jsonb THEN
            v_iterable := '[]'::jsonb;
        ELSIF pg_catalog.jsonb_typeof(v_batch.assigned_user_ids) = 'array' THEN
            v_iterable := v_batch.assigned_user_ids;
        ELSIF pg_catalog.jsonb_typeof(v_batch.assigned_user_ids) = 'object' THEN
            SELECT COALESCE(
                pg_catalog.jsonb_agg(pg_catalog.to_jsonb(entry.key)
                    ORDER BY pg_catalog.char_length(entry.key), entry.key COLLATE "C"),
                '[]'::jsonb
            )
            INTO v_iterable
            FROM pg_catalog.jsonb_object_keys(v_batch.assigned_user_ids)
                AS entry(key);
        ELSIF pg_catalog.jsonb_typeof(v_batch.assigned_user_ids) = 'string' THEN
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.to_jsonb(pg_catalog.substr(
                        source_text.assigned_text, positions.character_number, 1
                    )) ORDER BY positions.character_number
                ),
                '[]'::jsonb
            )
            INTO v_iterable
            FROM pg_catalog.generate_series(
                1,
                pg_catalog.char_length(v_batch.assigned_user_ids #>> '{}')
            ) AS positions(character_number)
            CROSS JOIN LATERAL (
                SELECT v_batch.assigned_user_ids #>> '{}' AS assigned_text
            ) AS source_text;
        ELSE
            RAISE EXCEPTION 'legacy assigned_user_ids is not iterable';
        END IF;

        v_updated := '[]'::jsonb;
        v_replaced := false;
        FOR v_item IN
            SELECT element.value
            FROM pg_catalog.jsonb_array_elements(v_iterable) AS element(value)
        LOOP
            CASE pg_catalog.jsonb_typeof(v_item)
                WHEN 'string' THEN v_string := v_item #>> '{}';
                WHEN 'null' THEN v_string := 'None';
                WHEN 'boolean' THEN
                    v_string := CASE WHEN v_item = 'true'::jsonb
                        THEN 'True' ELSE 'False' END;
                WHEN 'number' THEN v_string := v_item::text;
                WHEN 'object' THEN
                    v_string := pg_catalog.replace(v_item::text, '"', '''');
                    v_string := pg_catalog.replace(v_string, ': true', ': True');
                    v_string := pg_catalog.replace(v_string, ': false', ': False');
                    v_string := pg_catalog.replace(v_string, ': null', ': None');
                    v_string := pg_catalog.replace(v_string, ', true', ', True');
                    v_string := pg_catalog.replace(v_string, ', false', ', False');
                    v_string := pg_catalog.replace(v_string, ', null', ', None');
                WHEN 'array' THEN
                    v_string := pg_catalog.replace(v_item::text, '"', '''');
                    v_string := pg_catalog.replace(v_string, 'true', 'True');
                    v_string := pg_catalog.replace(v_string, 'false', 'False');
                    v_string := pg_catalog.replace(v_string, 'null', 'None');
                ELSE
                    RAISE EXCEPTION 'unsupported assigned_user_ids value';
            END CASE;

            IF v_string = p_from_user_id::text THEN
                v_string := p_to_user_id::text;
                v_replaced := true;
            END IF;
            v_updated := v_updated || pg_catalog.jsonb_build_array(v_string);
        END LOOP;

        IF NOT v_replaced THEN
            v_updated := v_updated || pg_catalog.jsonb_build_array(p_to_user_id::text);
        END IF;

        UPDATE public.task_batches AS batch
        SET reviewer_id = p_to_user_id,
            assigned_user_ids = v_updated,
            updated_at = pg_catalog.now()
        WHERE batch.id = v_batch.id
          AND batch.project_id = p_project_id;
    END LOOP;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.handoff_membership_reviewer(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.release_membership_task_locks(
    p_project_id uuid,
    p_user_id uuid
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted bigint;
BEGIN
    IF p_project_id IS NULL OR p_user_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    WITH active AS MATERIALIZED (
        SELECT task_lock.id
        FROM public.task_locks AS task_lock
        JOIN public.tasks AS task ON task.id = task_lock.task_id
        WHERE task.project_id = p_project_id
          AND task_lock.user_id = p_user_id
          AND task_lock.expire_at > pg_catalog.clock_timestamp()
    ), deleted AS (
        DELETE FROM public.task_locks AS task_lock
        USING active
        WHERE task_lock.id = active.id
        RETURNING task_lock.id
    )
    SELECT pg_catalog.count(*)::bigint INTO v_deleted FROM deleted;

    RETURN v_deleted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.release_membership_task_locks(uuid, uuid)
    FROM PUBLIC;
