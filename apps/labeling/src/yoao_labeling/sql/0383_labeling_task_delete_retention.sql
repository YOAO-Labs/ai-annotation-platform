-- Forward replacement of frozen 0355 bytes for Main's unified feedback rows.
--
-- `annotation_feedbacks` now references both `tasks` and `annotations` with
-- NO ACTION foreign keys, so `delete_source_less_children` must remove the
-- feedback rows it is about to orphan before deleting their annotation/task
-- parents. Thread replies that survive the frozen task scope are detached from
-- the deleted parents instead of blocking the delete. The operation set, task
-- scope validation, ordering of the remaining child deletes, counters and reply
-- shape are unchanged from 0355.
--
-- Comment attachment retirement is the caller's responsibility (the Labeling
-- discussion capability deletes those comments first); this command never
-- touches storage receipts.
CREATE OR REPLACE FUNCTION aap_labeling_v1.task_maintenance_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_schema text;
    v_project_id uuid;
    v_task_ids uuid[] := ARRAY[]::uuid[];
    v_batch_ids uuid[] := ARRAY[]::uuid[];
    v_keys text[];
    v_item jsonb;
    v_row record;
    v_total bigint;
    v_completed bigint;
    v_review bigint;
    v_in_progress bigint;
    v_annotation_count bigint;
    v_feedback_ids uuid[] := ARRAY[]::uuid[];
    v_deleted_count bigint;
    v_batches jsonb := '[]'::jsonb;
    v_task_ids_json jsonb := '[]'::jsonb;
    v_data jsonb;
    v_phase text := 'task_busy';
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'Invalid Labeling Task maintenance request' USING ERRCODE = '22023';
    END IF;
    v_schema := p_request ->> 'schema';
    v_operation := p_request ->> 'operation';
    IF v_schema IS DISTINCT FROM 'aap-labeling-task-maintenance/v1'
       OR v_operation IS NULL THEN
        RAISE EXCEPTION 'Invalid Labeling Task maintenance protocol' USING ERRCODE = '22023';
    END IF;

    IF v_operation IN ('read_source_less_task_candidates', 'read_source_less_project_counters', 'read_source_less_project_batches') THEN
        SELECT array_agg(key ORDER BY key) INTO v_keys FROM jsonb_object_keys(p_request) AS fields(key);
        IF v_keys IS DISTINCT FROM ARRAY['operation','project_id','schema']::text[]
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Labeling Task maintenance project scope' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
    ELSIF v_operation IN ('lock_source_less_tasks', 'delete_source_less_children', 'delete_source_less_task_records') THEN
        SELECT array_agg(key ORDER BY key) INTO v_keys FROM jsonb_object_keys(p_request) AS fields(key);
        IF v_keys IS DISTINCT FROM ARRAY['operation','project_id','schema','task_ids']::text[]
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'task_ids') IS DISTINCT FROM 'array'
           OR EXISTS (SELECT FROM jsonb_array_elements(p_request -> 'task_ids') AS item(value)
                      WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'string') THEN
            RAISE EXCEPTION 'Invalid Labeling Task maintenance task scope' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
        INTO v_task_ids
        FROM jsonb_array_elements_text(p_request -> 'task_ids') AS item(value);
        IF cardinality(v_task_ids) <> (SELECT count(DISTINCT id) FROM unnest(v_task_ids) AS item(id)) THEN
            RAISE EXCEPTION 'Duplicate Labeling Task maintenance identities' USING ERRCODE = '22023';
        END IF;
    ELSIF v_operation = 'read_source_less_batch_counters' THEN
        SELECT array_agg(key ORDER BY key) INTO v_keys FROM jsonb_object_keys(p_request) AS fields(key);
        IF v_keys IS DISTINCT FROM ARRAY['batch_ids','operation','project_id','schema']::text[]
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'batch_ids') IS DISTINCT FROM 'array'
           OR EXISTS (SELECT FROM jsonb_array_elements(p_request -> 'batch_ids') AS item(value)
                      WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'string') THEN
            RAISE EXCEPTION 'Invalid Labeling Task maintenance batch scope' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
        INTO v_batch_ids
        FROM jsonb_array_elements_text(p_request -> 'batch_ids') AS item(value);
        IF cardinality(v_batch_ids) <> (SELECT count(DISTINCT id) FROM unnest(v_batch_ids) AS item(id)) THEN
            RAISE EXCEPTION 'Duplicate Labeling Task maintenance batch identities' USING ERRCODE = '22023';
        END IF;
    ELSIF v_operation = 'write_source_less_batch_counters' THEN
        SELECT array_agg(key ORDER BY key) INTO v_keys FROM jsonb_object_keys(p_request) AS fields(key);
        IF v_keys IS DISTINCT FROM ARRAY['batches','operation','project_id','schema']::text[]
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'batches') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling Task maintenance batch write' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'batches') AS items(value) LOOP
            SELECT array_agg(key ORDER BY key) INTO v_keys FROM jsonb_object_keys(v_item) AS fields(key);
            IF v_keys IS DISTINCT FROM ARRAY['batch_id','completed_tasks','completed_tasks_set','review_tasks','review_tasks_set','total_tasks','total_tasks_set','updated_at','updated_at_set']::text[]
               OR jsonb_typeof(v_item -> 'batch_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_item -> 'total_tasks_set') IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(v_item -> 'completed_tasks_set') IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(v_item -> 'review_tasks_set') IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(v_item -> 'total_tasks') NOT IN ('number','null')
               OR jsonb_typeof(v_item -> 'completed_tasks') NOT IN ('number','null')
               OR jsonb_typeof(v_item -> 'review_tasks') NOT IN ('number','null')
               OR jsonb_typeof(v_item -> 'updated_at_set') IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(v_item -> 'updated_at') NOT IN ('string','null')
               OR (v_item ->> 'total_tasks_set')::boolean IS DISTINCT FROM (v_item -> 'total_tasks' <> 'null'::jsonb)
               OR (v_item ->> 'completed_tasks_set')::boolean IS DISTINCT FROM (v_item -> 'completed_tasks' <> 'null'::jsonb)
               OR (v_item ->> 'review_tasks_set')::boolean IS DISTINCT FROM (v_item -> 'review_tasks' <> 'null'::jsonb)
               OR ((v_item ->> 'total_tasks_set')::boolean AND ((v_item ->> 'total_tasks') !~ '^(0|[1-9][0-9]*)$' OR (v_item ->> 'total_tasks')::numeric > 2147483647))
               OR ((v_item ->> 'completed_tasks_set')::boolean AND ((v_item ->> 'completed_tasks') !~ '^(0|[1-9][0-9]*)$' OR (v_item ->> 'completed_tasks')::numeric > 2147483647))
               OR ((v_item ->> 'review_tasks_set')::boolean AND ((v_item ->> 'review_tasks') !~ '^(0|[1-9][0-9]*)$' OR (v_item ->> 'review_tasks')::numeric > 2147483647))
               OR ((v_item ->> 'updated_at_set')::boolean IS DISTINCT FROM (v_item -> 'updated_at' IS NOT NULL AND v_item -> 'updated_at' <> 'null'::jsonb)) THEN
                RAISE EXCEPTION 'Invalid Labeling Task maintenance batch counter' USING ERRCODE = '22023';
            END IF;
        END LOOP;
        SELECT COALESCE(array_agg((value ->> 'batch_id')::uuid), ARRAY[]::uuid[])
        INTO v_batch_ids
        FROM jsonb_array_elements(p_request -> 'batches') AS items(value);
        IF cardinality(v_batch_ids) <> (SELECT count(DISTINCT id) FROM unnest(v_batch_ids) AS item(id)) THEN
            RAISE EXCEPTION 'Duplicate Labeling Task maintenance batch identities' USING ERRCODE = '22023';
        END IF;
    ELSE
        RAISE EXCEPTION 'Unknown Labeling Task maintenance operation' USING ERRCODE = '22023';
    END IF;

    IF v_operation = 'read_source_less_task_candidates' THEN
        SELECT COALESCE(jsonb_agg(task.id ORDER BY task.id), '[]'::jsonb)
        INTO v_task_ids_json
        FROM public.tasks AS task
        WHERE task.project_id = v_project_id
          AND NOT EXISTS (
              SELECT FROM aap_assets_v1.project_dataset_item_sources AS source
              WHERE source.project_id = task.project_id
                AND source.item_id = task.dataset_item_id
          );
        v_data := jsonb_build_object('operation', v_operation, 'project_id', v_project_id, 'task_ids', v_task_ids_json);
    ELSIF v_operation = 'lock_source_less_tasks' THEN
        v_task_ids_json := '[]'::jsonb;
        FOR v_row IN
            SELECT task.id, task.project_id
            FROM public.tasks AS task
            WHERE task.id = ANY(v_task_ids)
            ORDER BY task.id
            FOR UPDATE
        LOOP
            IF v_row.project_id IS DISTINCT FROM v_project_id THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_labeling_task_maintenance_error', DETAIL = '{"reason":"task_target_changed"}';
            END IF;
            v_task_ids_json := v_task_ids_json || jsonb_build_array(v_row.id);
        END LOOP;
        v_data := jsonb_build_object('operation', v_operation, 'project_id', v_project_id, 'task_ids', v_task_ids_json);
    ELSIF v_operation = 'delete_source_less_children' THEN
        IF EXISTS (
            SELECT FROM public.tasks AS task
            WHERE task.id = ANY(v_task_ids)
              AND task.project_id IS DISTINCT FROM v_project_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_labeling_task_maintenance_error', DETAIL = '{"reason":"task_target_changed"}';
        END IF;
        SELECT count(*) INTO v_annotation_count
        FROM public.annotations AS annotation
        JOIN public.tasks AS task ON task.id = annotation.task_id
        WHERE annotation.task_id = ANY(v_task_ids)
          AND task.project_id = v_project_id;
        -- Unified feedback rows anchor at both Tasks and Annotations, so they
        -- must be removed inside the frozen target scope before their parents.
        SELECT COALESCE(array_agg(feedback.id ORDER BY feedback.id), ARRAY[]::uuid[])
        INTO v_feedback_ids
        FROM public.annotation_feedbacks AS feedback
        WHERE EXISTS (
            SELECT 1
            FROM public.tasks AS task
            WHERE task.id = ANY(v_task_ids)
              AND task.project_id = v_project_id
              AND (
                  feedback.task_id = task.id
                  OR feedback.annotation_id IN (
                      SELECT annotation.id
                      FROM public.annotations AS annotation
                      WHERE annotation.task_id = task.id
                  )
              )
        );
        IF cardinality(v_feedback_ids) > 0 THEN
            UPDATE public.annotation_feedbacks AS feedback
            SET thread_parent_id = NULL
            WHERE feedback.thread_parent_id = ANY(v_feedback_ids)
              AND NOT (feedback.id = ANY(v_feedback_ids));
            DELETE FROM public.annotation_feedbacks AS feedback
            WHERE feedback.id = ANY(v_feedback_ids);
        END IF;
        DELETE FROM public.annotation_drafts AS draft
        USING public.tasks AS task
        WHERE draft.task_id = ANY(v_task_ids)
          AND task.id = draft.task_id
          AND task.project_id = v_project_id;
        UPDATE public.annotations AS annotation
        SET parent_prediction_id = NULL,
            parent_annotation_id = NULL,
            updated_at = transaction_timestamp()
        FROM public.tasks AS task
        WHERE annotation.task_id = ANY(v_task_ids)
          AND task.id = annotation.task_id
          AND task.project_id = v_project_id;
        DELETE FROM public.annotations AS annotation
        USING public.tasks AS task
        WHERE annotation.task_id = ANY(v_task_ids)
          AND task.id = annotation.task_id
          AND task.project_id = v_project_id;
        DELETE FROM public.task_locks AS task_lock
        USING public.tasks AS task
        WHERE task_lock.task_id = ANY(v_task_ids)
          AND task.id = task_lock.task_id
          AND task.project_id = v_project_id;
        v_data := jsonb_build_object('operation', v_operation, 'project_id', v_project_id, 'task_ids', to_jsonb(v_task_ids), 'annotation_count', v_annotation_count);
    ELSIF v_operation = 'delete_source_less_task_records' THEN
        IF EXISTS (
            SELECT FROM public.tasks AS task
            WHERE task.id = ANY(v_task_ids)
              AND task.project_id IS DISTINCT FROM v_project_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_labeling_task_maintenance_error', DETAIL = '{"reason":"task_target_changed"}';
        END IF;
        DELETE FROM public.tasks AS task
        WHERE task.id = ANY(v_task_ids)
          AND task.project_id = v_project_id;
        GET DIAGNOSTICS v_deleted_count = ROW_COUNT;
        v_data := jsonb_build_object('operation', v_operation, 'project_id', v_project_id, 'task_ids', to_jsonb(v_task_ids), 'deleted_count', v_deleted_count);
    ELSIF v_operation = 'read_source_less_project_counters' THEN
        SELECT count(*),
               count(*) FILTER (WHERE task.status = 'completed'),
               count(*) FILTER (WHERE task.status = 'review'),
               count(*) FILTER (WHERE task.status = 'in_progress')
        INTO v_total, v_completed, v_review, v_in_progress
        FROM public.tasks AS task
        WHERE task.project_id = v_project_id;
        v_data := jsonb_build_object(
            'operation', v_operation,
            'project_id', v_project_id,
            'total_tasks', v_total,
            'completed_tasks', v_completed,
            'review_tasks', v_review,
            'in_progress_tasks', v_in_progress
        );
    ELSIF v_operation = 'read_source_less_project_batches' THEN
        FOR v_row IN
            SELECT batch.id, batch.project_id, batch.dataset_id, batch.display_id, batch.name,
                   batch.description, batch.status, batch.priority, batch.deadline,
                   batch.assigned_user_ids, batch.total_tasks, batch.completed_tasks,
                   batch.review_tasks, batch.approved_tasks, batch.rejected_tasks,
                   batch.created_by, batch.created_at, batch.updated_at, batch.review_feedback,
                   batch.reviewed_at, batch.reviewed_by, batch.annotator_id, batch.reviewer_id,
                   batch.admin_locked, batch.admin_lock_reason, batch.admin_locked_at,
                   batch.admin_locked_by
            FROM public.task_batches AS batch
            WHERE batch.project_id = v_project_id
        LOOP
            v_batches := v_batches || jsonb_build_array(jsonb_build_object(
                'id', v_row.id,
                'project_id', v_row.project_id,
                'dataset_id', v_row.dataset_id,
                'display_id', v_row.display_id,
                'name', v_row.name,
                'description', v_row.description,
                'status', v_row.status,
                'priority', v_row.priority,
                'deadline', v_row.deadline,
                'assigned_user_ids', v_row.assigned_user_ids,
                'total_tasks', v_row.total_tasks,
                'completed_tasks', v_row.completed_tasks,
                'review_tasks', v_row.review_tasks,
                'approved_tasks', v_row.approved_tasks,
                'rejected_tasks', v_row.rejected_tasks,
                'created_by', v_row.created_by,
                'created_at', v_row.created_at,
                'updated_at', v_row.updated_at,
                'review_feedback', v_row.review_feedback,
                'reviewed_at', v_row.reviewed_at,
                'reviewed_by', v_row.reviewed_by,
                'annotator_id', v_row.annotator_id,
                'reviewer_id', v_row.reviewer_id,
                'admin_locked', v_row.admin_locked,
                'admin_lock_reason', v_row.admin_lock_reason,
                'admin_locked_at', v_row.admin_locked_at,
                'admin_locked_by', v_row.admin_locked_by
            ));
        END LOOP;
        v_data := jsonb_build_object('operation', v_operation, 'project_id', v_project_id, 'batches', v_batches);
    ELSIF v_operation = 'read_source_less_batch_counters' THEN
        FOR v_row IN
            SELECT requested.id AS batch_id,
                   count(task.id) AS total_tasks,
                   count(task.id) FILTER (WHERE task.status = 'completed') AS completed_tasks,
                   count(task.id) FILTER (WHERE task.status = 'review') AS review_tasks
            FROM unnest(v_batch_ids) AS requested(id)
            JOIN public.task_batches AS batch
              ON batch.id = requested.id
             AND batch.project_id = v_project_id
            LEFT JOIN public.tasks AS task ON task.batch_id = requested.id
            GROUP BY requested.id
            ORDER BY requested.id
        LOOP
            v_batches := v_batches || jsonb_build_array(jsonb_build_object(
                'batch_id', v_row.batch_id,
                'total_tasks', v_row.total_tasks,
                'completed_tasks', v_row.completed_tasks,
                'review_tasks', v_row.review_tasks
            ));
        END LOOP;
        IF jsonb_array_length(v_batches) <> cardinality(v_batch_ids) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_labeling_task_maintenance_error', DETAIL = '{"reason":"batch_target_changed"}';
        END IF;
        v_data := jsonb_build_object('operation', v_operation, 'project_id', v_project_id, 'batches', v_batches);
    ELSE
        WITH patches AS (
            SELECT (item.value ->> 'batch_id')::uuid AS batch_id,
                   (item.value ->> 'total_tasks_set')::boolean AS total_tasks_set,
                   CASE WHEN (item.value ->> 'total_tasks_set')::boolean THEN (item.value ->> 'total_tasks')::integer END AS total_tasks,
                   (item.value ->> 'completed_tasks_set')::boolean AS completed_tasks_set,
                   CASE WHEN (item.value ->> 'completed_tasks_set')::boolean THEN (item.value ->> 'completed_tasks')::integer END AS completed_tasks,
                   (item.value ->> 'review_tasks_set')::boolean AS review_tasks_set,
                   CASE WHEN (item.value ->> 'review_tasks_set')::boolean THEN (item.value ->> 'review_tasks')::integer END AS review_tasks,
                   (item.value ->> 'updated_at_set')::boolean AS updated_at_set,
                   CASE WHEN item.value -> 'updated_at' = 'null'::jsonb THEN NULL
                        ELSE (item.value ->> 'updated_at')::timestamptz END AS updated_at
            FROM jsonb_array_elements(p_request -> 'batches') AS item(value)
        ), updated AS (
            UPDATE public.task_batches AS batch
            SET total_tasks = CASE WHEN patch.total_tasks_set THEN patch.total_tasks ELSE batch.total_tasks END,
                completed_tasks = CASE WHEN patch.completed_tasks_set THEN patch.completed_tasks ELSE batch.completed_tasks END,
                review_tasks = CASE WHEN patch.review_tasks_set THEN patch.review_tasks ELSE batch.review_tasks END,
                updated_at = CASE WHEN patch.updated_at_set THEN patch.updated_at ELSE transaction_timestamp() END
            FROM patches AS patch
            WHERE batch.id = patch.batch_id
              AND batch.project_id = v_project_id
            RETURNING batch.id, batch.project_id, batch.total_tasks,
                      batch.completed_tasks, batch.review_tasks, batch.updated_at
        )
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'batch_id', updated.id,
            'project_id', updated.project_id,
            'total_tasks', updated.total_tasks,
            'completed_tasks', updated.completed_tasks,
            'review_tasks', updated.review_tasks,
            'updated_at', updated.updated_at
        ) ORDER BY updated.id), '[]'::jsonb)
        INTO v_batches
        FROM updated;
        IF jsonb_array_length(v_batches) <> cardinality(v_batch_ids) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_labeling_task_maintenance_error', DETAIL = '{"reason":"batch_target_changed"}';
        END IF;
        v_data := jsonb_build_object('operation', v_operation, 'project_id', v_project_id, 'batches', v_batches);
    END IF;

    RETURN jsonb_build_object(
        'schema', 'aap-labeling-task-maintenance/v1',
        'ok', true,
        'request', p_request,
        'data', v_data
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_labeling_task_maintenance_error',
            DETAIL = jsonb_build_object('reason', v_phase)::text;
    WHEN invalid_text_representation OR numeric_value_out_of_range
         OR invalid_datetime_format OR datetime_field_overflow THEN
        RAISE EXCEPTION 'Invalid Labeling Task maintenance value' USING ERRCODE = '22023';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.task_maintenance_command(jsonb) FROM PUBLIC;
