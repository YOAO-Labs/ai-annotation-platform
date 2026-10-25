CREATE VIEW aap_labeling_v1.annotation_comment_facts
WITH (security_barrier = true)
AS
SELECT id, annotation_id, project_id, author_id, body, is_resolved, is_active,
       mentions, attachments, canvas_drawing, anchor, created_at, updated_at
FROM public.annotation_comments;
-- aap-statement-break
REVOKE ALL ON aap_labeling_v1.annotation_comment_facts FROM PUBLIC;
-- aap-statement-break
CREATE VIEW aap_labeling_v1.annotation_feedback_facts
WITH (security_barrier = true)
AS
SELECT id, kind, anchor_type, project_id, task_id, annotation_id, anchor_position,
       status, severity, title, body, author_id, attachments, mentions,
       canvas_drawing, thread_parent_id, is_active, resolved_at, resolved_by_id,
       created_at, updated_at
FROM public.annotation_feedbacks;
-- aap-statement-break
REVOKE ALL ON aap_labeling_v1.annotation_feedback_facts FROM PUBLIC;
-- aap-statement-break
CREATE VIEW aap_labeling_v1.task_event_facts
WITH (security_barrier = true)
AS
SELECT event.id, event.task_id, event.user_id, event.project_id, event.kind,
       event.started_at, event.ended_at, event.duration_ms,
       event.annotation_count, event.was_rejected, event.collector_version,
       event.collection_source, event.collection_coverage, event.created_at,
       task.reject_reason_type
FROM public.task_events AS event
LEFT JOIN public.tasks AS task ON task.id = event.task_id;
-- aap-statement-break
REVOKE ALL ON aap_labeling_v1.task_event_facts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.discussion_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_keys text[];
    v_expected_keys text[];
    v_comment public.annotation_comments%ROWTYPE;
    v_feedback public.annotation_feedbacks%ROWTYPE;
    v_data jsonb;
    v_feedbacks jsonb;
    v_deleted integer;
    v_now timestamptz := transaction_timestamp();
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-discussion/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Labeling discussion request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'create_comment' THEN
            v_expected_keys := ARRAY['schema', 'operation', 'comment_id', 'annotation_id', 'project_id', 'author_id', 'body', 'mentions', 'attachments', 'canvas_drawing', 'anchor'];
        WHEN 'lock_comment', 'soft_delete_comment', 'clear_comment_attachments' THEN
            v_expected_keys := ARRAY['schema', 'operation', 'comment_id'];
        WHEN 'delete_comments_for_tasks' THEN
            v_expected_keys := ARRAY['schema', 'operation', 'task_ids'];
        WHEN 'update_comment' THEN
            v_expected_keys := ARRAY['schema', 'operation', 'comment_id'];
            IF p_request ? 'body' THEN v_expected_keys := v_expected_keys || ARRAY['body']; END IF;
            IF p_request ? 'is_resolved' THEN v_expected_keys := v_expected_keys || ARRAY['is_resolved']; END IF;
        WHEN 'create_feedback' THEN
            v_expected_keys := ARRAY['schema', 'operation', 'feedback_id', 'author_id', 'kind', 'anchor_type', 'project_id', 'task_id', 'annotation_id', 'anchor_position', 'severity', 'title', 'body', 'attachments', 'thread_parent_id', 'canvas_drawing', 'mentions'];
        WHEN 'patch_feedback' THEN
            v_expected_keys := ARRAY['schema', 'operation', 'feedback_id', 'actor_id'];
            FOREACH v_operation IN ARRAY ARRAY['status', 'resolved_at', 'severity', 'title', 'body', 'mentions'] LOOP
                IF p_request ? v_operation THEN v_expected_keys := v_expected_keys || ARRAY[v_operation]; END IF;
            END LOOP;
            v_operation := p_request ->> 'operation';
        WHEN 'soft_delete_feedback' THEN
            v_expected_keys := ARRAY['schema', 'operation', 'feedback_id'];
        WHEN 'lock_feedback_lineage' THEN
            v_expected_keys := ARRAY['schema', 'operation', 'feedback_ids'];
        WHEN 'mirror_bug_report' THEN
            v_expected_keys := ARRAY['schema', 'operation', 'feedback_id', 'project_id', 'task_id', 'author_id', 'severity', 'title', 'body'];
        WHEN 'mirror_annotation_comment' THEN
            v_expected_keys := ARRAY['schema', 'operation', 'feedback_id', 'comment_id', 'annotation_id', 'task_id', 'project_id', 'author_id', 'body', 'attachments', 'mentions'];
        WHEN 'mirror_task_reject' THEN
            v_expected_keys := ARRAY['schema', 'operation', 'feedback_id', 'task_id', 'project_id', 'reviewer_id', 'severity', 'body'];
        ELSE
            RAISE EXCEPTION 'Invalid Labeling discussion operation' USING ERRCODE = '22023';
    END CASE;
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS fields(key);
    SELECT array_agg(key ORDER BY key) INTO v_expected_keys
    FROM unnest(v_expected_keys) AS fields(key);
    IF v_keys IS DISTINCT FROM v_expected_keys THEN
        RAISE EXCEPTION 'Invalid Labeling discussion fields' USING ERRCODE = '22023';
    END IF;

    IF v_operation = 'create_comment' THEN
        IF jsonb_typeof(p_request -> 'body') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'mentions') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'attachments') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'canvas_drawing') NOT IN ('object', 'null')
           OR jsonb_typeof(p_request -> 'anchor') NOT IN ('object', 'null')
           OR char_length(p_request ->> 'body') > 4000 THEN
            RAISE EXCEPTION 'Invalid Annotation comment payload' USING ERRCODE = '22023';
        END IF;
        IF btrim(p_request ->> 'body') = ''
           AND jsonb_array_length(p_request -> 'attachments') = 0
           AND COALESCE(jsonb_array_length(p_request -> 'canvas_drawing' -> 'shapes'), 0) = 0 THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_labeling_discussion_error',
                DETAIL = jsonb_build_object('status_code', 422,
                    'detail', '评论需至少包含正文、附件或画布批注之一')::text;
        END IF;
        INSERT INTO public.annotation_comments (
            id, annotation_id, project_id, author_id, body, mentions, attachments,
            canvas_drawing, anchor
        ) VALUES (
            (p_request ->> 'comment_id')::uuid,
            (p_request ->> 'annotation_id')::uuid,
            (p_request ->> 'project_id')::uuid,
            (p_request ->> 'author_id')::uuid,
            p_request ->> 'body', p_request -> 'mentions', p_request -> 'attachments',
            p_request -> 'canvas_drawing', p_request -> 'anchor'
        ) RETURNING * INTO v_comment;
        v_data := jsonb_build_object('comment', to_jsonb(v_comment));
    ELSIF v_operation IN ('lock_comment', 'update_comment', 'soft_delete_comment') THEN
        SELECT comment.* INTO v_comment
        FROM public.annotation_comments AS comment
        WHERE comment.id = (p_request ->> 'comment_id')::uuid
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_labeling_discussion_error',
                DETAIL = jsonb_build_object('status_code', 404, 'detail', 'Comment not found')::text;
        END IF;
        IF v_operation = 'update_comment' THEN
            IF p_request ? 'body' AND jsonb_typeof(p_request -> 'body') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid comment body' USING ERRCODE = '22023';
            END IF;
            IF p_request ? 'is_resolved' AND jsonb_typeof(p_request -> 'is_resolved') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION 'Invalid comment resolution' USING ERRCODE = '22023';
            END IF;
            IF p_request ? 'body' AND (
                char_length(p_request ->> 'body') > 4000
                OR (btrim(p_request ->> 'body') = ''
                    AND jsonb_array_length(v_comment.attachments) = 0
                    AND COALESCE(jsonb_array_length(v_comment.canvas_drawing -> 'shapes'), 0) = 0)
            ) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_labeling_discussion_error',
                    DETAIL = jsonb_build_object('status_code', 422,
                        'detail', '评论需至少包含正文、附件或画布批注之一')::text;
            END IF;
            IF (p_request ? 'body' AND p_request ->> 'body' IS DISTINCT FROM v_comment.body)
               OR (p_request ? 'is_resolved' AND (p_request ->> 'is_resolved')::boolean IS DISTINCT FROM v_comment.is_resolved) THEN
                UPDATE public.annotation_comments AS comment
                SET body = CASE WHEN p_request ? 'body' THEN p_request ->> 'body' ELSE comment.body END,
                    is_resolved = CASE WHEN p_request ? 'is_resolved' THEN (p_request ->> 'is_resolved')::boolean ELSE comment.is_resolved END,
                    updated_at = v_now
                WHERE comment.id = v_comment.id
                RETURNING * INTO v_comment;
            END IF;
        ELSIF v_operation = 'soft_delete_comment' THEN
            UPDATE public.annotation_comments AS comment
            SET is_active = false, updated_at = v_now
            WHERE comment.id = v_comment.id
            RETURNING * INTO v_comment;
        END IF;
        v_data := jsonb_build_object('comment', to_jsonb(v_comment));
    ELSIF v_operation = 'clear_comment_attachments' THEN
        UPDATE public.annotation_comments AS comment
        SET attachments = '[]'::jsonb, updated_at = v_now
        WHERE comment.id = (p_request ->> 'comment_id')::uuid;
        v_data := '{}'::jsonb;
    ELSIF v_operation = 'delete_comments_for_tasks' THEN
        IF jsonb_typeof(p_request -> 'task_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid comment task IDs' USING ERRCODE = '22023';
        END IF;
        DELETE FROM public.annotation_comments AS comment
        USING public.annotations AS annotation
        WHERE annotation.id = comment.annotation_id
          AND annotation.task_id IN (
              SELECT value::uuid
              FROM jsonb_array_elements_text(p_request -> 'task_ids')
          );
        GET DIAGNOSTICS v_deleted = ROW_COUNT;
        v_data := jsonb_build_object('deleted_comments', v_deleted);
    ELSIF v_operation IN ('create_feedback', 'mirror_bug_report', 'mirror_annotation_comment', 'mirror_task_reject') THEN
        IF v_operation = 'create_feedback' THEN
            IF jsonb_typeof(p_request -> 'attachments') IS DISTINCT FROM 'array'
               OR jsonb_typeof(p_request -> 'mentions') IS DISTINCT FROM 'array'
               OR jsonb_typeof(p_request -> 'body') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'anchor_position') NOT IN ('object', 'null')
                OR jsonb_typeof(p_request -> 'canvas_drawing') NOT IN ('object', 'null')
                OR p_request ->> 'kind' NOT IN ('issue', 'comment', 'reject', 'bug')
                OR p_request ->> 'anchor_type' NOT IN ('project', 'task', 'annotation', 'pixel', 'point_cloud')
                OR char_length(COALESCE(p_request ->> 'severity', '')) > 16
                OR char_length(COALESCE(p_request ->> 'title', '')) > 500 THEN
                RAISE EXCEPTION 'Invalid Annotation feedback payload' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.annotation_feedbacks (
                id, kind, anchor_type, project_id, task_id, annotation_id,
                anchor_position, status, severity, title, body, author_id,
                attachments, mentions, canvas_drawing, thread_parent_id, is_active
            ) VALUES (
                (p_request ->> 'feedback_id')::uuid,
                p_request ->> 'kind', p_request ->> 'anchor_type',
                (p_request ->> 'project_id')::uuid,
                (p_request ->> 'task_id')::uuid,
                (p_request ->> 'annotation_id')::uuid,
                CASE WHEN jsonb_typeof(p_request -> 'anchor_position') = 'null' THEN NULL ELSE p_request -> 'anchor_position' END,
                'open', (p_request ->> 'severity'), (p_request ->> 'title'),
                p_request ->> 'body', (p_request ->> 'author_id')::uuid,
                p_request -> 'attachments', p_request -> 'mentions',
                p_request -> 'canvas_drawing', (p_request ->> 'thread_parent_id')::uuid, true
            ) RETURNING * INTO v_feedback;
        ELSIF v_operation = 'mirror_bug_report' THEN
            IF jsonb_typeof(p_request -> 'body') IS DISTINCT FROM 'string'
               OR char_length(COALESCE(p_request ->> 'severity', '')) > 16
               OR char_length(COALESCE(p_request ->> 'title', '')) > 500 THEN
                RAISE EXCEPTION 'Invalid mirrored bug report fields' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.annotation_feedbacks (
                id, kind, anchor_type, project_id, task_id, annotation_id,
                anchor_position, status, severity, title, body, author_id,
                attachments, mentions, thread_parent_id, is_active
            ) VALUES (
                (p_request ->> 'feedback_id')::uuid, 'bug',
                CASE WHEN p_request -> 'task_id' = 'null'::jsonb THEN 'project' ELSE 'task' END,
                (p_request ->> 'project_id')::uuid, (p_request ->> 'task_id')::uuid,
                NULL, NULL, 'open', p_request ->> 'severity', p_request ->> 'title',
                p_request ->> 'body', (p_request ->> 'author_id')::uuid,
                '[]'::jsonb, '[]'::jsonb, NULL, true
            ) RETURNING * INTO v_feedback;
        ELSIF v_operation = 'mirror_annotation_comment' THEN
            IF jsonb_typeof(p_request -> 'attachments') IS DISTINCT FROM 'array'
               OR jsonb_typeof(p_request -> 'mentions') IS DISTINCT FROM 'array'
               OR jsonb_typeof(p_request -> 'body') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid mirrored comment payload' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.annotation_feedbacks (
                id, kind, anchor_type, project_id, task_id, annotation_id,
                anchor_position, status, severity, title, body, author_id,
                attachments, mentions, thread_parent_id, is_active
            ) VALUES (
                (p_request ->> 'feedback_id')::uuid, 'comment', 'annotation',
                (p_request ->> 'project_id')::uuid, (p_request ->> 'task_id')::uuid,
                (p_request ->> 'annotation_id')::uuid, NULL, 'open', NULL, NULL,
                p_request ->> 'body', (p_request ->> 'author_id')::uuid,
                p_request -> 'attachments', p_request -> 'mentions', NULL, true
            ) RETURNING * INTO v_feedback;
        ELSE
            IF jsonb_typeof(p_request -> 'body') IS DISTINCT FROM 'string'
               OR char_length(COALESCE(p_request ->> 'severity', '')) > 16 THEN
                RAISE EXCEPTION 'Invalid mirrored task rejection fields' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.annotation_feedbacks (
                id, kind, anchor_type, project_id, task_id, annotation_id,
                anchor_position, status, severity, title, body, author_id,
                attachments, mentions, thread_parent_id, is_active
            ) VALUES (
                (p_request ->> 'feedback_id')::uuid, 'reject', 'task',
                (p_request ->> 'project_id')::uuid, (p_request ->> 'task_id')::uuid,
                NULL, NULL, 'open', p_request ->> 'severity', NULL,
                p_request ->> 'body', (p_request ->> 'reviewer_id')::uuid,
                '[]'::jsonb, '[]'::jsonb, NULL, true
            ) RETURNING * INTO v_feedback;
        END IF;
        v_data := jsonb_build_object('feedback', to_jsonb(v_feedback));
    ELSIF v_operation = 'lock_feedback_lineage' THEN
        IF jsonb_typeof(p_request -> 'feedback_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid feedback lineage IDs' USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(jsonb_agg(to_jsonb(locked) ORDER BY locked.id), '[]'::jsonb)
        INTO v_feedbacks
        FROM (
            SELECT feedback.*
            FROM public.annotation_feedbacks AS feedback
            WHERE feedback.id IN (
                SELECT value::uuid
                FROM jsonb_array_elements_text(p_request -> 'feedback_ids')
            )
            ORDER BY feedback.id
            FOR UPDATE NOWAIT
        ) AS locked;
        v_data := jsonb_build_object('feedbacks', v_feedbacks);
    ELSIF v_operation IN ('patch_feedback', 'soft_delete_feedback') THEN
        SELECT feedback.* INTO v_feedback
        FROM public.annotation_feedbacks AS feedback
        WHERE feedback.id = (p_request ->> 'feedback_id')::uuid
        FOR UPDATE NOWAIT;
        IF NOT FOUND OR v_feedback.is_active IS NOT TRUE THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_labeling_discussion_error',
                DETAIL = jsonb_build_object('status_code', 404, 'detail', 'feedback not found')::text;
        END IF;
        IF v_operation = 'soft_delete_feedback' THEN
            UPDATE public.annotation_feedbacks AS feedback
            SET is_active = false, updated_at = v_now
            WHERE feedback.id = v_feedback.id
            RETURNING * INTO v_feedback;
        ELSE
            IF p_request ? 'status' AND jsonb_typeof(p_request -> 'status') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid feedback status' USING ERRCODE = '22023';
            END IF;
            IF p_request ? 'severity' AND jsonb_typeof(p_request -> 'severity') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid feedback severity' USING ERRCODE = '22023';
            END IF;
            IF p_request ? 'title' AND jsonb_typeof(p_request -> 'title') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid feedback title' USING ERRCODE = '22023';
            END IF;
            IF p_request ? 'body' AND jsonb_typeof(p_request -> 'body') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid feedback body' USING ERRCODE = '22023';
            END IF;
            IF p_request ? 'resolved_at' AND jsonb_typeof(p_request -> 'resolved_at') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid feedback resolution timestamp' USING ERRCODE = '22023';
            END IF;
            IF p_request ? 'mentions' AND jsonb_typeof(p_request -> 'mentions') IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION 'Invalid feedback mentions' USING ERRCODE = '22023';
            END IF;
            IF (p_request ? 'status' AND p_request ->> 'status' NOT IN ('open', 'resolved', 'wont_fix'))
               OR (p_request ? 'severity' AND char_length(p_request ->> 'severity') > 16)
               OR (p_request ? 'title' AND char_length(p_request ->> 'title') > 500) THEN
                RAISE EXCEPTION 'Invalid feedback patch values' USING ERRCODE = '22023';
            END IF;
            IF p_request ? 'status'
               AND p_request ->> 'status' IS DISTINCT FROM v_feedback.status
               AND p_request ->> 'status' IN ('resolved', 'wont_fix')
               AND (NOT (p_request ? 'resolved_at') OR NOT (p_request ? 'actor_id')) THEN
                RAISE EXCEPTION 'Feedback resolution requires its UTC actor timestamp' USING ERRCODE = '22023';
            END IF;
            IF (p_request ? 'status' AND p_request ->> 'status' IS DISTINCT FROM v_feedback.status)
               OR (p_request ? 'severity' AND p_request ->> 'severity' IS DISTINCT FROM v_feedback.severity)
               OR (p_request ? 'title' AND p_request ->> 'title' IS DISTINCT FROM v_feedback.title)
               OR (p_request ? 'body' AND p_request ->> 'body' IS DISTINCT FROM v_feedback.body)
               OR (p_request ? 'mentions' AND p_request -> 'mentions' IS DISTINCT FROM v_feedback.mentions) THEN
                UPDATE public.annotation_feedbacks AS feedback
                SET status = CASE WHEN p_request ? 'status' THEN p_request ->> 'status' ELSE feedback.status END,
                    resolved_at = CASE
                        WHEN p_request ? 'status' AND p_request ->> 'status' IS DISTINCT FROM feedback.status
                            AND p_request ->> 'status' IN ('resolved', 'wont_fix')
                            THEN (p_request ->> 'resolved_at')::timestamptz
                        WHEN p_request ? 'status' AND p_request ->> 'status' IS DISTINCT FROM feedback.status THEN NULL
                        ELSE feedback.resolved_at END,
                    resolved_by_id = CASE
                        WHEN p_request ? 'status' AND p_request ->> 'status' IS DISTINCT FROM feedback.status
                            AND p_request ->> 'status' IN ('resolved', 'wont_fix')
                            THEN (p_request ->> 'actor_id')::uuid
                        WHEN p_request ? 'status' AND p_request ->> 'status' IS DISTINCT FROM feedback.status THEN NULL
                        ELSE feedback.resolved_by_id END,
                    severity = CASE WHEN p_request ? 'severity' THEN p_request ->> 'severity' ELSE feedback.severity END,
                    title = CASE WHEN p_request ? 'title' THEN p_request ->> 'title' ELSE feedback.title END,
                    body = CASE WHEN p_request ? 'body' THEN p_request ->> 'body' ELSE feedback.body END,
                    mentions = CASE
                        WHEN p_request ? 'mentions' THEN p_request -> 'mentions'
                        WHEN p_request ? 'body' AND p_request ->> 'body' IS DISTINCT FROM feedback.body THEN '[]'::jsonb
                        ELSE feedback.mentions END,
                    updated_at = v_now
                WHERE feedback.id = v_feedback.id
                RETURNING * INTO v_feedback;
            END IF;
        END IF;
        v_data := jsonb_build_object('feedback', to_jsonb(v_feedback));
    END IF;
    RETURN jsonb_build_object(
        'schema', 'aap-labeling-discussion/v1', 'ok', true,
        'request', p_request, 'data', v_data
    );
EXCEPTION
    WHEN lock_not_available THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_labeling_discussion_error',
            DETAIL = jsonb_build_object('status_code', 409,
                'detail', jsonb_build_object('reason', CASE
                    WHEN p_request ->> 'operation' LIKE '%comment%' THEN 'comment_busy'
                    ELSE 'feedback_busy' END))::text;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.discussion_command(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.task_event_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_keys text[];
    v_expected_keys text[];
    v_conflicts uuid[] := ARRAY[]::uuid[];
    v_id uuid;
    v_inserted integer := 0;
    v_rows jsonb;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-task-events/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid TaskEvent request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'read_ids' THEN v_expected_keys := ARRAY['schema', 'operation', 'event_ids'];
        WHEN 'insert' THEN v_expected_keys := ARRAY['schema', 'operation', 'rows'];
        ELSE RAISE EXCEPTION 'Invalid TaskEvent operation' USING ERRCODE = '22023';
    END CASE;
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS fields(key);
    SELECT array_agg(key ORDER BY key) INTO v_expected_keys
    FROM unnest(v_expected_keys) AS fields(key);
    IF v_keys IS DISTINCT FROM v_expected_keys THEN
        RAISE EXCEPTION 'Invalid TaskEvent request fields' USING ERRCODE = '22023';
    END IF;
    IF v_operation = 'read_ids' THEN
        IF jsonb_typeof(p_request -> 'event_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid TaskEvent IDs' USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(jsonb_agg(to_jsonb(event) ORDER BY event.id), '[]'::jsonb)
        INTO v_rows
        FROM public.task_events AS event
        WHERE event.id IN (SELECT value::text::uuid FROM jsonb_array_elements_text(p_request -> 'event_ids'));
        RETURN jsonb_build_object('schema', 'aap-labeling-task-events/v1', 'ok', true,
            'request', p_request, 'data', jsonb_build_object('event_rows', v_rows));
    END IF;
    IF jsonb_typeof(p_request -> 'rows') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'Invalid TaskEvent rows' USING ERRCODE = '22023';
    END IF;
    IF jsonb_array_length(p_request -> 'rows') > 200 THEN
        RAISE EXCEPTION 'TaskEvent batch is too large' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_request -> 'rows') AS input(value)
        WHERE jsonb_typeof(input.value) IS DISTINCT FROM 'object'
           OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(input.value) AS fields(key)) IS DISTINCT FROM
              ARRAY['annotation_count', 'collection_coverage', 'collection_source', 'collector_version', 'duration_ms', 'ended_at', 'id', 'kind', 'project_id', 'started_at', 'task_id', 'user_id', 'was_rejected']::text[]
           OR jsonb_typeof(input.value -> 'id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(input.value -> 'task_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(input.value -> 'user_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(input.value -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(input.value -> 'kind') IS DISTINCT FROM 'string'
           OR input.value ->> 'kind' NOT IN ('annotate', 'review')
           OR jsonb_typeof(input.value -> 'started_at') IS DISTINCT FROM 'string'
           OR (input.value ->> 'started_at') !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
           OR jsonb_typeof(input.value -> 'ended_at') IS DISTINCT FROM 'string'
           OR (input.value ->> 'ended_at') !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
           OR jsonb_typeof(input.value -> 'duration_ms') IS DISTINCT FROM 'number'
           OR (input.value ->> 'duration_ms') !~ '^[0-9]+$'
           OR (input.value ->> 'duration_ms')::numeric > 14400000
           OR jsonb_typeof(input.value -> 'annotation_count') IS DISTINCT FROM 'number'
           OR (input.value ->> 'annotation_count') !~ '^[0-9]+$'
           OR (input.value ->> 'annotation_count')::numeric > 1000000
           OR jsonb_typeof(input.value -> 'was_rejected') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(input.value -> 'collector_version') NOT IN ('string', 'null')
           OR char_length(COALESCE(input.value ->> 'collector_version', '')) > 32
           OR jsonb_typeof(input.value -> 'collection_source') IS DISTINCT FROM 'string'
           OR input.value ->> 'collection_source' NOT IN ('session', 'legacy')
           OR jsonb_typeof(input.value -> 'collection_coverage') IS DISTINCT FROM 'string'
           OR input.value ->> 'collection_coverage' NOT IN ('qualified', 'partial', 'unverified_collection')
    ) THEN
        RAISE EXCEPTION 'Invalid TaskEvent row fields' USING ERRCODE = '22023';
    END IF;
    FOR v_id IN
        SELECT DISTINCT (input.value ->> 'id')::uuid
        FROM jsonb_array_elements(p_request -> 'rows') AS input(value)
        ORDER BY 1
    LOOP
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended('aap:task-event:' || v_id::text, 0)
        );
    END LOOP;
    WITH input_rows AS (
        SELECT input.*, raw.ordinality
        FROM jsonb_array_elements(p_request -> 'rows') WITH ORDINALITY AS raw(value, ordinality)
        CROSS JOIN LATERAL jsonb_to_record(raw.value) AS input(
            id uuid, task_id uuid, user_id uuid, project_id uuid, kind text,
            started_at timestamptz, ended_at timestamptz, duration_ms integer,
            annotation_count integer, was_rejected boolean,
            collector_version text, collection_source text, collection_coverage text
        )
    ), duplicate_conflicts AS (
        SELECT input_rows.id
        FROM input_rows
        GROUP BY input_rows.id
        HAVING COUNT(DISTINCT jsonb_build_array(
            input_rows.task_id, input_rows.user_id, input_rows.project_id, input_rows.kind,
            input_rows.started_at, input_rows.ended_at, input_rows.duration_ms,
            input_rows.annotation_count, input_rows.was_rejected,
            input_rows.collector_version, input_rows.collection_coverage
        )) > 1
    )
    SELECT COALESCE(array_agg(id ORDER BY id), ARRAY[]::uuid[])
    INTO v_conflicts FROM duplicate_conflicts;
    WITH input_rows AS (
        SELECT input.*, raw.ordinality
        FROM jsonb_array_elements(p_request -> 'rows') WITH ORDINALITY AS raw(value, ordinality)
        CROSS JOIN LATERAL jsonb_to_record(raw.value) AS input(
            id uuid, task_id uuid, user_id uuid, project_id uuid, kind text,
            started_at timestamptz, ended_at timestamptz, duration_ms integer,
            annotation_count integer, was_rejected boolean,
            collector_version text, collection_source text, collection_coverage text
        )
    ), latest AS (
        SELECT DISTINCT ON (input_rows.id) input_rows.*
        FROM input_rows
        ORDER BY input_rows.id, input_rows.ordinality DESC
    ), conflicting_stored AS (
        SELECT latest.id
        FROM latest JOIN public.task_events AS stored ON stored.id = latest.id
        WHERE (stored.task_id, stored.user_id, stored.project_id, stored.kind,
               stored.started_at, stored.ended_at, stored.duration_ms,
               stored.annotation_count, stored.was_rejected,
               stored.collector_version, stored.collection_coverage)
              IS DISTINCT FROM
              (latest.task_id, latest.user_id, latest.project_id, latest.kind,
               latest.started_at, latest.ended_at, latest.duration_ms,
               latest.annotation_count, latest.was_rejected,
               latest.collector_version, latest.collection_coverage)
    )
    SELECT COALESCE(array_agg(DISTINCT conflict.id ORDER BY conflict.id), ARRAY[]::uuid[])
    INTO v_conflicts
    FROM (
        SELECT unnest(v_conflicts) AS id
        UNION
        SELECT conflicting_stored.id FROM conflicting_stored
    ) AS conflict;
    WITH input_rows AS (
        SELECT input.*, raw.ordinality
        FROM jsonb_array_elements(p_request -> 'rows') WITH ORDINALITY AS raw(value, ordinality)
        CROSS JOIN LATERAL jsonb_to_record(raw.value) AS input(
            id uuid, task_id uuid, user_id uuid, project_id uuid, kind text,
            started_at timestamptz, ended_at timestamptz, duration_ms integer,
            annotation_count integer, was_rejected boolean,
            collector_version text, collection_source text, collection_coverage text
        )
    ), latest AS (
        SELECT DISTINCT ON (input_rows.id) input_rows.*
        FROM input_rows
        ORDER BY input_rows.id, input_rows.ordinality DESC
    ), inserted AS (
        INSERT INTO public.task_events (
            id, task_id, user_id, project_id, kind, started_at, ended_at,
            duration_ms, annotation_count, was_rejected, collector_version,
            collection_source, collection_coverage
        )
        SELECT latest.id, latest.task_id, latest.user_id, latest.project_id,
               latest.kind, latest.started_at, latest.ended_at, latest.duration_ms,
               latest.annotation_count, latest.was_rejected, latest.collector_version,
               latest.collection_source, latest.collection_coverage
        FROM latest
        WHERE NOT latest.id = ANY(v_conflicts)
          AND NOT EXISTS (SELECT 1 FROM public.task_events AS stored WHERE stored.id = latest.id)
        RETURNING 1
    ) SELECT COUNT(*)::integer INTO v_inserted FROM inserted;
    RETURN jsonb_build_object('schema', 'aap-labeling-task-events/v1', 'ok', true,
        'request', p_request,
        'data', jsonb_build_object('inserted', v_inserted, 'conflicts', to_jsonb(v_conflicts)));
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.task_event_command(jsonb) FROM PUBLIC;
