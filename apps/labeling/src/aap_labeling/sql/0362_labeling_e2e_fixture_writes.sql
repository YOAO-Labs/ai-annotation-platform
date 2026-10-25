CREATE FUNCTION aap_labeling_v1.e2e_fixture_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_operation text;
    v_project_id uuid;
    v_task_id uuid;
    v_feedback_id uuid;
    v_ids jsonb := '[]'::jsonb;
    v_anchor_position jsonb;
    v_rows jsonb;
    v_row jsonb;
    v_pending jsonb;
    v_index integer;
    v_remaining integer;
    v_progress boolean;
    v_parent_id uuid;
    v_row_id uuid;
    v_task_project_id uuid;
    v_existing_anchor jsonb;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE = '42501',
            MESSAGE = 'Labeling E2E fixture writes require a test database';
    END IF;
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-e2e-fixtures/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string'
       OR pg_catalog.octet_length(p_request::text) > 16777216 THEN
        RAISE EXCEPTION 'Invalid Labeling E2E fixture request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';

    IF v_operation = 'insert_batches' THEN
        IF (p_request - ARRAY['project_id','operation','rows','schema']) <> '{}'::jsonb
           OR NOT (p_request ?& ARRAY['project_id','operation','rows','schema'])
           OR pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR (p_request ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'rows') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') < 1
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') > 512 THEN
            RAISE EXCEPTION 'Invalid Labeling E2E fixture Batch request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        FOR v_row IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'rows') AS rows(value) LOOP
            IF pg_catalog.jsonb_typeof(v_row) IS DISTINCT FROM 'object'
               OR (v_row - ARRAY[
                    'id','project_id','display_id','name','status','assigned_user_ids',
                    'total_tasks','created_by','annotator_id','reviewer_id'
               ]) <> '{}'::jsonb
               OR NOT (v_row ?& ARRAY['id','project_id','display_id','name'])
               OR pg_catalog.jsonb_typeof(v_row -> 'id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'project_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'display_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'name') IS DISTINCT FROM 'string'
               OR (v_row ->> 'id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'project_id')::uuid IS DISTINCT FROM v_project_id
               OR pg_catalog.char_length(v_row ->> 'display_id') NOT BETWEEN 1 AND 30
               OR pg_catalog.char_length(v_row ->> 'name') NOT BETWEEN 1 AND 255 THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Batch row' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'status' AND pg_catalog.jsonb_typeof(v_row -> 'status') NOT IN ('string','null'))
               OR (v_row ? 'assigned_user_ids' AND pg_catalog.jsonb_typeof(v_row -> 'assigned_user_ids') NOT IN ('array','null'))
               OR (v_row ? 'total_tasks' AND pg_catalog.jsonb_typeof(v_row -> 'total_tasks') NOT IN ('number','null'))
               OR (v_row ? 'created_by' AND pg_catalog.jsonb_typeof(v_row -> 'created_by') NOT IN ('string','null'))
               OR (v_row ? 'annotator_id' AND pg_catalog.jsonb_typeof(v_row -> 'annotator_id') NOT IN ('string','null'))
               OR (v_row ? 'reviewer_id' AND pg_catalog.jsonb_typeof(v_row -> 'reviewer_id') NOT IN ('string','null')) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Batch field types' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'status' AND v_row ->> 'status' IS NOT NULL
                AND v_row ->> 'status' NOT IN ('draft','annotating'))
               OR (v_row ? 'status' AND pg_catalog.char_length(v_row ->> 'status') > 30)
               OR (v_row ? 'assigned_user_ids' AND pg_catalog.jsonb_typeof(v_row -> 'assigned_user_ids') = 'array'
                   AND EXISTS (
                       SELECT FROM pg_catalog.jsonb_array_elements_text(v_row -> 'assigned_user_ids') AS item(value)
                       WHERE item.value !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   )) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Batch value' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'created_by' AND v_row ->> 'created_by' IS NOT NULL AND (v_row ->> 'created_by') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'annotator_id' AND v_row ->> 'annotator_id' IS NOT NULL AND (v_row ->> 'annotator_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'reviewer_id' AND v_row ->> 'reviewer_id' IS NOT NULL AND (v_row ->> 'reviewer_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'assigned_user_ids' AND pg_catalog.jsonb_typeof(v_row -> 'assigned_user_ids') = 'array'
                   AND EXISTS (SELECT FROM pg_catalog.jsonb_array_elements(v_row -> 'assigned_user_ids') AS item(value)
                               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string')) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Batch identifiers' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'total_tasks' AND pg_catalog.jsonb_typeof(v_row -> 'total_tasks') = 'number'
                AND ((v_row ->> 'total_tasks') !~ '^(0|[1-9][0-9]*)$' OR (v_row ->> 'total_tasks')::numeric > 2147483647))
               THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Batch counter' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.task_batches (
                id, project_id, display_id, name, status, assigned_user_ids,
                total_tasks, created_by, annotator_id, reviewer_id
            ) VALUES (
                (v_row ->> 'id')::uuid, v_project_id, v_row ->> 'display_id',
                v_row ->> 'name',
                CASE WHEN v_row ? 'status' THEN v_row ->> 'status' ELSE 'draft' END,
                CASE WHEN v_row ? 'assigned_user_ids' THEN v_row -> 'assigned_user_ids' ELSE '[]'::jsonb END,
                CASE WHEN v_row ? 'total_tasks' THEN (v_row ->> 'total_tasks')::integer ELSE 0 END,
                CASE WHEN v_row ? 'created_by' THEN (v_row ->> 'created_by')::uuid ELSE NULL END,
                CASE WHEN v_row ? 'annotator_id' THEN (v_row ->> 'annotator_id')::uuid ELSE NULL END,
                CASE WHEN v_row ? 'reviewer_id' THEN (v_row ->> 'reviewer_id')::uuid ELSE NULL END
            ) RETURNING id INTO v_row_id;
            v_ids := v_ids || pg_catalog.jsonb_build_array(v_row_id);
        END LOOP;

    ELSIF v_operation = 'insert_tasks' THEN
        IF (p_request - ARRAY['project_id','operation','rows','schema']) <> '{}'::jsonb
           OR NOT (p_request ?& ARRAY['project_id','operation','rows','schema'])
           OR pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR (p_request ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'rows') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') < 1
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') > 512 THEN
            RAISE EXCEPTION 'Invalid Labeling E2E fixture Task request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        FOR v_row IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'rows') AS rows(value) LOOP
            IF pg_catalog.jsonb_typeof(v_row) IS DISTINCT FROM 'object'
               OR (v_row - ARRAY[
                    'id','project_id','display_id','file_name','file_path','dataset_item_id',
                    'batch_id','file_type','status','sequence_order','created_at','updated_at',
                    'assignee_id','assigned_at','reviewer_id','reviewer_claimed_at','submitted_at',
                    'reviewed_at','review_round_id','annotation_contributor_ids','is_labeled',
                    'total_annotations','total_predictions','reject_reason','reject_reason_type',
                    'skip_reason','skipped_at'
               ]) <> '{}'::jsonb
               OR NOT (v_row ?& ARRAY['id','project_id','display_id','file_name','file_path'])
               OR pg_catalog.jsonb_typeof(v_row -> 'id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'project_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'display_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'file_name') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'file_path') IS DISTINCT FROM 'string'
               OR (v_row ->> 'id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'project_id')::uuid IS DISTINCT FROM v_project_id
               OR pg_catalog.char_length(v_row ->> 'display_id') NOT BETWEEN 1 AND 30
               OR pg_catalog.char_length(v_row ->> 'file_name') NOT BETWEEN 1 AND 500
               OR pg_catalog.char_length(v_row ->> 'file_path') NOT BETWEEN 1 AND 1000 THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Task row' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'dataset_item_id' AND pg_catalog.jsonb_typeof(v_row -> 'dataset_item_id') NOT IN ('string','null'))
               OR (v_row ? 'batch_id' AND pg_catalog.jsonb_typeof(v_row -> 'batch_id') NOT IN ('string','null'))
               OR (v_row ? 'file_type' AND pg_catalog.jsonb_typeof(v_row -> 'file_type') NOT IN ('string','null'))
               OR (v_row ? 'status' AND pg_catalog.jsonb_typeof(v_row -> 'status') NOT IN ('string','null'))
               OR (v_row ? 'sequence_order' AND pg_catalog.jsonb_typeof(v_row -> 'sequence_order') NOT IN ('number','null'))
               OR (v_row ? 'created_at' AND pg_catalog.jsonb_typeof(v_row -> 'created_at') NOT IN ('string','null'))
               OR (v_row ? 'updated_at' AND pg_catalog.jsonb_typeof(v_row -> 'updated_at') NOT IN ('string','null'))
               OR (v_row ? 'assignee_id' AND pg_catalog.jsonb_typeof(v_row -> 'assignee_id') NOT IN ('string','null'))
               OR (v_row ? 'assigned_at' AND pg_catalog.jsonb_typeof(v_row -> 'assigned_at') NOT IN ('string','null'))
               OR (v_row ? 'reviewer_id' AND pg_catalog.jsonb_typeof(v_row -> 'reviewer_id') NOT IN ('string','null'))
               OR (v_row ? 'reviewer_claimed_at' AND pg_catalog.jsonb_typeof(v_row -> 'reviewer_claimed_at') NOT IN ('string','null'))
               OR (v_row ? 'submitted_at' AND pg_catalog.jsonb_typeof(v_row -> 'submitted_at') NOT IN ('string','null'))
               OR (v_row ? 'reviewed_at' AND pg_catalog.jsonb_typeof(v_row -> 'reviewed_at') NOT IN ('string','null'))
               OR (v_row ? 'review_round_id' AND pg_catalog.jsonb_typeof(v_row -> 'review_round_id') NOT IN ('string','null'))
               OR (v_row ? 'annotation_contributor_ids' AND pg_catalog.jsonb_typeof(v_row -> 'annotation_contributor_ids') NOT IN ('array','null'))
               OR (v_row ? 'is_labeled' AND pg_catalog.jsonb_typeof(v_row -> 'is_labeled') NOT IN ('boolean','null'))
               OR (v_row ? 'total_annotations' AND pg_catalog.jsonb_typeof(v_row -> 'total_annotations') NOT IN ('number','null'))
               OR (v_row ? 'total_predictions' AND pg_catalog.jsonb_typeof(v_row -> 'total_predictions') NOT IN ('number','null'))
               OR (v_row ? 'reject_reason' AND pg_catalog.jsonb_typeof(v_row -> 'reject_reason') NOT IN ('string','null'))
               OR (v_row ? 'reject_reason_type' AND pg_catalog.jsonb_typeof(v_row -> 'reject_reason_type') NOT IN ('string','null'))
               OR (v_row ? 'skip_reason' AND pg_catalog.jsonb_typeof(v_row -> 'skip_reason') NOT IN ('string','null'))
               OR (v_row ? 'skipped_at' AND pg_catalog.jsonb_typeof(v_row -> 'skipped_at') NOT IN ('string','null')) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Task field types' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'file_type' AND v_row ->> 'file_type' IS NOT NULL
                AND v_row ->> 'file_type' NOT IN ('image','video','point_cloud'))
               OR (v_row ? 'status' AND v_row ->> 'status' IS NOT NULL
                AND v_row ->> 'status' NOT IN ('pending','annotating','submitted','review','completed','rejected'))
               OR (v_row ? 'sequence_order' AND pg_catalog.jsonb_typeof(v_row -> 'sequence_order') = 'number'
                AND ((v_row ->> 'sequence_order') !~ '^-?[0-9]+$' OR (v_row ->> 'sequence_order')::numeric NOT BETWEEN -2147483648 AND 2147483647))
               OR (v_row ? 'total_annotations' AND pg_catalog.jsonb_typeof(v_row -> 'total_annotations') = 'number'
                AND ((v_row ->> 'total_annotations') !~ '^(0|[1-9][0-9]*)$' OR (v_row ->> 'total_annotations')::numeric > 2147483647))
               OR (v_row ? 'total_predictions' AND pg_catalog.jsonb_typeof(v_row -> 'total_predictions') = 'number'
                AND ((v_row ->> 'total_predictions') !~ '^(0|[1-9][0-9]*)$' OR (v_row ->> 'total_predictions')::numeric > 2147483647))
               THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Task value' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'dataset_item_id' AND v_row ->> 'dataset_item_id' IS NOT NULL AND (v_row ->> 'dataset_item_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'batch_id' AND v_row ->> 'batch_id' IS NOT NULL AND (v_row ->> 'batch_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'assignee_id' AND v_row ->> 'assignee_id' IS NOT NULL AND (v_row ->> 'assignee_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'reviewer_id' AND v_row ->> 'reviewer_id' IS NOT NULL AND (v_row ->> 'reviewer_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'review_round_id' AND v_row ->> 'review_round_id' IS NOT NULL AND (v_row ->> 'review_round_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'annotation_contributor_ids' AND pg_catalog.jsonb_typeof(v_row -> 'annotation_contributor_ids') = 'array'
                   AND EXISTS (SELECT FROM pg_catalog.jsonb_array_elements(v_row -> 'annotation_contributor_ids') AS item(value)
                               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                                  OR (item.value #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Task identifiers' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'reject_reason' AND v_row ->> 'reject_reason' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'reject_reason') > 2000)
               OR (v_row ? 'reject_reason_type' AND v_row ->> 'reject_reason_type' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'reject_reason_type') > 20)
               OR (v_row ? 'skip_reason' AND v_row ->> 'skip_reason' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'skip_reason') > 50) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Task text field' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.tasks (
                id, project_id, dataset_item_id, display_id, file_name, file_path,
                tags, file_type, status, batch_id, sequence_order, created_at, updated_at,
                assignee_id, assigned_at, reviewer_id, reviewer_claimed_at,
                submitted_at, reviewed_at, review_round_id,
                annotation_contributor_ids, is_labeled, overlap,
                total_annotations, total_predictions, reject_reason,
                reject_reason_type, skip_reason, skipped_at, assignee_is_override,
                reviewer_is_override, version, first_review_eligible, reopened_count
            ) VALUES (
                (v_row ->> 'id')::uuid, v_project_id,
                CASE WHEN v_row ? 'dataset_item_id' THEN (v_row ->> 'dataset_item_id')::uuid ELSE NULL END,
                v_row ->> 'display_id', v_row ->> 'file_name', v_row ->> 'file_path',
                '[]'::jsonb,
                CASE WHEN v_row ? 'file_type' THEN v_row ->> 'file_type' ELSE 'image' END,
                CASE WHEN v_row ? 'status' THEN v_row ->> 'status' ELSE 'pending' END,
                CASE WHEN v_row ? 'batch_id' THEN (v_row ->> 'batch_id')::uuid ELSE NULL END,
                CASE WHEN v_row ? 'sequence_order' THEN (v_row ->> 'sequence_order')::integer ELSE NULL END,
                CASE WHEN v_row ? 'created_at' THEN (v_row ->> 'created_at')::timestamptz ELSE pg_catalog.transaction_timestamp() END,
                CASE WHEN v_row ? 'updated_at' THEN (v_row ->> 'updated_at')::timestamptz ELSE pg_catalog.transaction_timestamp() END,
                CASE WHEN v_row ? 'assignee_id' THEN (v_row ->> 'assignee_id')::uuid ELSE NULL END,
                CASE WHEN v_row ? 'assigned_at' THEN (v_row ->> 'assigned_at')::timestamptz ELSE NULL END,
                CASE WHEN v_row ? 'reviewer_id' THEN (v_row ->> 'reviewer_id')::uuid ELSE NULL END,
                CASE WHEN v_row ? 'reviewer_claimed_at' THEN (v_row ->> 'reviewer_claimed_at')::timestamptz ELSE NULL END,
                CASE WHEN v_row ? 'submitted_at' THEN (v_row ->> 'submitted_at')::timestamptz ELSE NULL END,
                CASE WHEN v_row ? 'reviewed_at' THEN (v_row ->> 'reviewed_at')::timestamptz ELSE NULL END,
                CASE WHEN v_row ? 'review_round_id' THEN (v_row ->> 'review_round_id')::uuid ELSE NULL END,
                CASE WHEN v_row ? 'annotation_contributor_ids' THEN v_row -> 'annotation_contributor_ids' ELSE '[]'::jsonb END,
                CASE WHEN v_row ? 'is_labeled' THEN (v_row ->> 'is_labeled')::boolean ELSE false END,
                1,
                CASE WHEN v_row ? 'total_annotations' THEN (v_row ->> 'total_annotations')::integer ELSE 0 END,
                CASE WHEN v_row ? 'total_predictions' THEN (v_row ->> 'total_predictions')::integer ELSE 0 END,
                CASE WHEN v_row ? 'reject_reason' THEN v_row ->> 'reject_reason' ELSE NULL END,
                CASE WHEN v_row ? 'reject_reason_type' THEN v_row ->> 'reject_reason_type' ELSE NULL END,
                CASE WHEN v_row ? 'skip_reason' THEN v_row ->> 'skip_reason' ELSE NULL END,
                CASE WHEN v_row ? 'skipped_at' THEN (v_row ->> 'skipped_at')::timestamptz ELSE NULL END,
                false, false, 1, true, 0
            ) RETURNING id INTO v_row_id;
            v_ids := v_ids || pg_catalog.jsonb_build_array(v_row_id);
        END LOOP;

    ELSIF v_operation = 'patch_tasks' THEN
        IF (p_request - ARRAY['project_id','operation','rows','schema']) <> '{}'::jsonb
           OR NOT (p_request ?& ARRAY['project_id','operation','rows','schema'])
           OR pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR (p_request ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'rows') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') < 1
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') > 512 THEN
            RAISE EXCEPTION 'Invalid Labeling E2E fixture Task patch request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        FOR v_row IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'rows') AS rows(value) LOOP
            IF pg_catalog.jsonb_typeof(v_row) IS DISTINCT FROM 'object'
               OR (v_row - ARRAY[
                    'id','project_id','batch_id','dataset_item_id','file_name','file_path',
                    'file_type','status','sequence_order','created_at','updated_at',
                    'assignee_id','assigned_at','reviewer_id','reviewer_claimed_at',
                    'submitted_at','reviewed_at','review_round_id','annotation_contributor_ids',
                    'is_labeled','total_annotations','total_predictions','reject_reason',
                    'reject_reason_type','skip_reason','skipped_at'
               ]) <> '{}'::jsonb
               OR NOT (v_row ?& ARRAY['id','project_id'])
               OR pg_catalog.jsonb_typeof(v_row -> 'id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'project_id') IS DISTINCT FROM 'string'
               OR (v_row ->> 'id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'project_id')::uuid IS DISTINCT FROM v_project_id
               OR (v_row - ARRAY['id','project_id']) = '{}'::jsonb THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Task patch row' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'batch_id' AND pg_catalog.jsonb_typeof(v_row -> 'batch_id') NOT IN ('string','null'))
               OR (v_row ? 'dataset_item_id' AND pg_catalog.jsonb_typeof(v_row -> 'dataset_item_id') NOT IN ('string','null'))
               OR (v_row ? 'file_name' AND pg_catalog.jsonb_typeof(v_row -> 'file_name') NOT IN ('string','null'))
               OR (v_row ? 'file_path' AND pg_catalog.jsonb_typeof(v_row -> 'file_path') NOT IN ('string','null'))
               OR (v_row ? 'file_type' AND pg_catalog.jsonb_typeof(v_row -> 'file_type') NOT IN ('string','null'))
               OR (v_row ? 'status' AND pg_catalog.jsonb_typeof(v_row -> 'status') NOT IN ('string','null'))
               OR (v_row ? 'sequence_order' AND pg_catalog.jsonb_typeof(v_row -> 'sequence_order') NOT IN ('number','null'))
               OR (v_row ? 'created_at' AND pg_catalog.jsonb_typeof(v_row -> 'created_at') NOT IN ('string','null'))
               OR (v_row ? 'updated_at' AND pg_catalog.jsonb_typeof(v_row -> 'updated_at') NOT IN ('string','null'))
               OR (v_row ? 'assignee_id' AND pg_catalog.jsonb_typeof(v_row -> 'assignee_id') NOT IN ('string','null'))
               OR (v_row ? 'assigned_at' AND pg_catalog.jsonb_typeof(v_row -> 'assigned_at') NOT IN ('string','null'))
               OR (v_row ? 'reviewer_id' AND pg_catalog.jsonb_typeof(v_row -> 'reviewer_id') NOT IN ('string','null'))
               OR (v_row ? 'reviewer_claimed_at' AND pg_catalog.jsonb_typeof(v_row -> 'reviewer_claimed_at') NOT IN ('string','null'))
               OR (v_row ? 'submitted_at' AND pg_catalog.jsonb_typeof(v_row -> 'submitted_at') NOT IN ('string','null'))
               OR (v_row ? 'reviewed_at' AND pg_catalog.jsonb_typeof(v_row -> 'reviewed_at') NOT IN ('string','null'))
               OR (v_row ? 'review_round_id' AND pg_catalog.jsonb_typeof(v_row -> 'review_round_id') NOT IN ('string','null'))
               OR (v_row ? 'annotation_contributor_ids' AND pg_catalog.jsonb_typeof(v_row -> 'annotation_contributor_ids') NOT IN ('array','null'))
               OR (v_row ? 'is_labeled' AND pg_catalog.jsonb_typeof(v_row -> 'is_labeled') NOT IN ('boolean','null'))
               OR (v_row ? 'total_annotations' AND pg_catalog.jsonb_typeof(v_row -> 'total_annotations') NOT IN ('number','null'))
               OR (v_row ? 'total_predictions' AND pg_catalog.jsonb_typeof(v_row -> 'total_predictions') NOT IN ('number','null'))
               OR (v_row ? 'reject_reason' AND pg_catalog.jsonb_typeof(v_row -> 'reject_reason') NOT IN ('string','null'))
               OR (v_row ? 'reject_reason_type' AND pg_catalog.jsonb_typeof(v_row -> 'reject_reason_type') NOT IN ('string','null'))
               OR (v_row ? 'skip_reason' AND pg_catalog.jsonb_typeof(v_row -> 'skip_reason') NOT IN ('string','null'))
               OR (v_row ? 'skipped_at' AND pg_catalog.jsonb_typeof(v_row -> 'skipped_at') NOT IN ('string','null')) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Task patch field types' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'file_name' AND v_row ->> 'file_name' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'file_name') NOT BETWEEN 1 AND 500)
               OR (v_row ? 'file_path' AND v_row ->> 'file_path' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'file_path') NOT BETWEEN 1 AND 1000)
               OR (v_row ? 'file_type' AND v_row ->> 'file_type' IS NOT NULL AND v_row ->> 'file_type' NOT IN ('image','video','point_cloud'))
               OR (v_row ? 'status' AND v_row ->> 'status' IS NOT NULL AND v_row ->> 'status' NOT IN ('pending','annotating','submitted','review','completed','rejected'))
               OR (v_row ? 'sequence_order' AND pg_catalog.jsonb_typeof(v_row -> 'sequence_order') = 'number'
                   AND ((v_row ->> 'sequence_order') !~ '^-?[0-9]+$' OR (v_row ->> 'sequence_order')::numeric NOT BETWEEN -2147483648 AND 2147483647))
               OR (v_row ? 'total_annotations' AND pg_catalog.jsonb_typeof(v_row -> 'total_annotations') = 'number'
                   AND ((v_row ->> 'total_annotations') !~ '^(0|[1-9][0-9]*)$' OR (v_row ->> 'total_annotations')::numeric > 2147483647))
               OR (v_row ? 'total_predictions' AND pg_catalog.jsonb_typeof(v_row -> 'total_predictions') = 'number'
                   AND ((v_row ->> 'total_predictions') !~ '^(0|[1-9][0-9]*)$' OR (v_row ->> 'total_predictions')::numeric > 2147483647))
               OR (v_row ? 'annotation_contributor_ids' AND pg_catalog.jsonb_typeof(v_row -> 'annotation_contributor_ids') = 'array'
                   AND EXISTS (SELECT FROM pg_catalog.jsonb_array_elements(v_row -> 'annotation_contributor_ids') AS item(value)
                               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                                  OR (item.value #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'))
               OR EXISTS (
                    SELECT FROM pg_catalog.jsonb_each(v_row) AS field(name, value)
                    WHERE field.name IN ('batch_id','dataset_item_id','assignee_id','reviewer_id','review_round_id')
                      AND pg_catalog.jsonb_typeof(field.value) = 'string'
                      AND (field.value #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               )
               OR (v_row ? 'reject_reason' AND v_row ->> 'reject_reason' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'reject_reason') > 2000)
               OR (v_row ? 'reject_reason_type' AND v_row ->> 'reject_reason_type' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'reject_reason_type') > 20)
               OR (v_row ? 'skip_reason' AND v_row ->> 'skip_reason' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'skip_reason') > 50) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Task patch value' USING ERRCODE = '22023';
            END IF;
            v_row_id := (v_row ->> 'id')::uuid;
            IF NOT EXISTS (SELECT FROM public.tasks AS task WHERE task.id=v_row_id AND task.project_id=v_project_id) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_labeling_e2e_fixture_error',
                    DETAIL = pg_catalog.jsonb_build_object('reason','task_target_changed')::text;
            END IF;
            UPDATE public.tasks AS task SET
                batch_id = CASE WHEN v_row ? 'batch_id' THEN (v_row ->> 'batch_id')::uuid ELSE task.batch_id END,
                dataset_item_id = CASE WHEN v_row ? 'dataset_item_id' THEN (v_row ->> 'dataset_item_id')::uuid ELSE task.dataset_item_id END,
                file_name = CASE WHEN v_row ? 'file_name' THEN v_row ->> 'file_name' ELSE task.file_name END,
                file_path = CASE WHEN v_row ? 'file_path' THEN v_row ->> 'file_path' ELSE task.file_path END,
                file_type = CASE WHEN v_row ? 'file_type' THEN v_row ->> 'file_type' ELSE task.file_type END,
                status = CASE WHEN v_row ? 'status' THEN v_row ->> 'status' ELSE task.status END,
                sequence_order = CASE WHEN v_row ? 'sequence_order' THEN (v_row ->> 'sequence_order')::integer ELSE task.sequence_order END,
                created_at = CASE WHEN v_row ? 'created_at' THEN (v_row ->> 'created_at')::timestamptz ELSE task.created_at END,
                assigned_at = CASE WHEN v_row ? 'assigned_at' THEN (v_row ->> 'assigned_at')::timestamptz ELSE task.assigned_at END,
                assignee_id = CASE WHEN v_row ? 'assignee_id' THEN (v_row ->> 'assignee_id')::uuid ELSE task.assignee_id END,
                reviewer_id = CASE WHEN v_row ? 'reviewer_id' THEN (v_row ->> 'reviewer_id')::uuid ELSE task.reviewer_id END,
                reviewer_claimed_at = CASE WHEN v_row ? 'reviewer_claimed_at' THEN (v_row ->> 'reviewer_claimed_at')::timestamptz ELSE task.reviewer_claimed_at END,
                submitted_at = CASE WHEN v_row ? 'submitted_at' THEN (v_row ->> 'submitted_at')::timestamptz ELSE task.submitted_at END,
                reviewed_at = CASE WHEN v_row ? 'reviewed_at' THEN (v_row ->> 'reviewed_at')::timestamptz ELSE task.reviewed_at END,
                review_round_id = CASE WHEN v_row ? 'review_round_id' THEN (v_row ->> 'review_round_id')::uuid ELSE task.review_round_id END,
                annotation_contributor_ids = CASE WHEN v_row ? 'annotation_contributor_ids' THEN v_row -> 'annotation_contributor_ids' ELSE task.annotation_contributor_ids END,
                is_labeled = CASE WHEN v_row ? 'is_labeled' THEN (v_row ->> 'is_labeled')::boolean ELSE task.is_labeled END,
                total_annotations = CASE WHEN v_row ? 'total_annotations' THEN (v_row ->> 'total_annotations')::integer ELSE task.total_annotations END,
                total_predictions = CASE WHEN v_row ? 'total_predictions' THEN (v_row ->> 'total_predictions')::integer ELSE task.total_predictions END,
                reject_reason = CASE WHEN v_row ? 'reject_reason' THEN v_row ->> 'reject_reason' ELSE task.reject_reason END,
                reject_reason_type = CASE WHEN v_row ? 'reject_reason_type' THEN v_row ->> 'reject_reason_type' ELSE task.reject_reason_type END,
                skip_reason = CASE WHEN v_row ? 'skip_reason' THEN v_row ->> 'skip_reason' ELSE task.skip_reason END,
                skipped_at = CASE WHEN v_row ? 'skipped_at' THEN (v_row ->> 'skipped_at')::timestamptz ELSE task.skipped_at END,
                updated_at = CASE WHEN v_row ? 'updated_at' THEN (v_row ->> 'updated_at')::timestamptz ELSE pg_catalog.transaction_timestamp() END
            WHERE task.id=v_row_id AND task.project_id=v_project_id;
            v_ids := v_ids || pg_catalog.jsonb_build_array(v_row_id);
        END LOOP;

    ELSIF v_operation = 'patch_batches' THEN
        IF (p_request - ARRAY['project_id','operation','rows','schema']) <> '{}'::jsonb
           OR NOT (p_request ?& ARRAY['project_id','operation','rows','schema'])
           OR pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR (p_request ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'rows') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') < 1
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') > 512 THEN
            RAISE EXCEPTION 'Invalid Labeling E2E fixture Batch patch request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        FOR v_row IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'rows') AS rows(value) LOOP
            IF pg_catalog.jsonb_typeof(v_row) IS DISTINCT FROM 'object'
               OR (v_row - ARRAY['id','project_id','total_tasks']) <> '{}'::jsonb
               OR NOT (v_row ?& ARRAY['id','project_id'])
               OR pg_catalog.jsonb_typeof(v_row -> 'id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'project_id') IS DISTINCT FROM 'string'
               OR (v_row ->> 'id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'project_id')::uuid IS DISTINCT FROM v_project_id
               OR (v_row - ARRAY['id','project_id']) = '{}'::jsonb THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Batch patch row' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'total_tasks' AND pg_catalog.jsonb_typeof(v_row -> 'total_tasks') NOT IN ('number','null'))
               OR (v_row ? 'total_tasks' AND pg_catalog.jsonb_typeof(v_row -> 'total_tasks') = 'number'
                   AND ((v_row ->> 'total_tasks') !~ '^(0|[1-9][0-9]*)$' OR (v_row ->> 'total_tasks')::numeric > 2147483647)) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Batch patch value' USING ERRCODE = '22023';
            END IF;
            v_row_id := (v_row ->> 'id')::uuid;
            IF NOT EXISTS (SELECT FROM public.task_batches AS batch WHERE batch.id=v_row_id AND batch.project_id=v_project_id) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_labeling_e2e_fixture_error',
                    DETAIL = pg_catalog.jsonb_build_object('reason','batch_target_changed')::text;
            END IF;
            UPDATE public.task_batches AS batch SET
                total_tasks = CASE WHEN v_row ? 'total_tasks' THEN (v_row ->> 'total_tasks')::integer ELSE batch.total_tasks END,
                updated_at = pg_catalog.transaction_timestamp()
            WHERE batch.id=v_row_id AND batch.project_id=v_project_id;
            v_ids := v_ids || pg_catalog.jsonb_build_array(v_row_id);
        END LOOP;

    ELSIF v_operation = 'insert_annotations' THEN
        IF (p_request - ARRAY['project_id','operation','rows','schema']) <> '{}'::jsonb
           OR NOT (p_request ?& ARRAY['project_id','operation','rows','schema'])
           OR pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR (p_request ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'rows') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') < 1
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') > 512 THEN
            RAISE EXCEPTION 'Invalid Labeling E2E fixture Annotation request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        FOR v_row IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'rows') AS rows(value) LOOP
            IF pg_catalog.jsonb_typeof(v_row) IS DISTINCT FROM 'object'
               OR (v_row - ARRAY[
                    'id','task_id','project_id','user_id','source','annotation_type',
                    'tool_unit_id','class_name','geometry','confidence','parent_prediction_id',
                    'parent_annotation_id','track_id','scene_track_id','temporal_role','lead_time',
                    'was_cancelled','ground_truth','is_active','attributes','is_locked',
                    'created_at','updated_at'
               ]) <> '{}'::jsonb
               OR NOT (v_row ?& ARRAY['id','task_id','project_id','class_name','geometry'])
               OR pg_catalog.jsonb_typeof(v_row -> 'id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'task_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'project_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'class_name') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'geometry') IS DISTINCT FROM 'object'
               OR (v_row ->> 'id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'task_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'project_id')::uuid IS DISTINCT FROM v_project_id
               OR pg_catalog.char_length(v_row ->> 'class_name') NOT BETWEEN 1 AND 100 THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Annotation row' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'user_id' AND pg_catalog.jsonb_typeof(v_row -> 'user_id') NOT IN ('string','null'))
               OR (v_row ? 'source' AND pg_catalog.jsonb_typeof(v_row -> 'source') NOT IN ('string','null'))
               OR (v_row ? 'annotation_type' AND pg_catalog.jsonb_typeof(v_row -> 'annotation_type') NOT IN ('string','null'))
               OR (v_row ? 'tool_unit_id' AND pg_catalog.jsonb_typeof(v_row -> 'tool_unit_id') NOT IN ('string','null'))
               OR (v_row ? 'confidence' AND pg_catalog.jsonb_typeof(v_row -> 'confidence') NOT IN ('number','null'))
               OR (v_row ? 'parent_prediction_id' AND pg_catalog.jsonb_typeof(v_row -> 'parent_prediction_id') NOT IN ('string','null'))
               OR (v_row ? 'parent_annotation_id' AND pg_catalog.jsonb_typeof(v_row -> 'parent_annotation_id') NOT IN ('string','null'))
               OR (v_row ? 'track_id' AND pg_catalog.jsonb_typeof(v_row -> 'track_id') NOT IN ('string','null'))
               OR (v_row ? 'scene_track_id' AND pg_catalog.jsonb_typeof(v_row -> 'scene_track_id') NOT IN ('string','null'))
               OR (v_row ? 'temporal_role' AND pg_catalog.jsonb_typeof(v_row -> 'temporal_role') NOT IN ('string','null'))
               OR (v_row ? 'lead_time' AND pg_catalog.jsonb_typeof(v_row -> 'lead_time') NOT IN ('number','null'))
               OR (v_row ? 'was_cancelled' AND pg_catalog.jsonb_typeof(v_row -> 'was_cancelled') NOT IN ('boolean','null'))
               OR (v_row ? 'ground_truth' AND pg_catalog.jsonb_typeof(v_row -> 'ground_truth') NOT IN ('boolean','null'))
               OR (v_row ? 'is_active' AND pg_catalog.jsonb_typeof(v_row -> 'is_active') NOT IN ('boolean','null'))
               OR (v_row ? 'attributes' AND pg_catalog.jsonb_typeof(v_row -> 'attributes') NOT IN ('object','null'))
               OR (v_row ? 'is_locked' AND pg_catalog.jsonb_typeof(v_row -> 'is_locked') NOT IN ('boolean','null'))
               OR (v_row ? 'created_at' AND pg_catalog.jsonb_typeof(v_row -> 'created_at') NOT IN ('string','null'))
               OR (v_row ? 'updated_at' AND pg_catalog.jsonb_typeof(v_row -> 'updated_at') NOT IN ('string','null')) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Annotation field types' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'user_id' AND pg_catalog.jsonb_typeof(v_row -> 'user_id') = 'string'
                AND (v_row ->> 'user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'parent_prediction_id' AND pg_catalog.jsonb_typeof(v_row -> 'parent_prediction_id') = 'string'
                AND (v_row ->> 'parent_prediction_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'parent_annotation_id' AND pg_catalog.jsonb_typeof(v_row -> 'parent_annotation_id') = 'string'
                AND (v_row ->> 'parent_annotation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'scene_track_id' AND pg_catalog.jsonb_typeof(v_row -> 'scene_track_id') = 'string'
                AND (v_row ->> 'scene_track_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'source' AND v_row ->> 'source' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'source') > 20)
               OR (v_row ? 'annotation_type' AND v_row ->> 'annotation_type' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'annotation_type') > 30)
               OR (v_row ? 'tool_unit_id' AND v_row ->> 'tool_unit_id' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'tool_unit_id') > 30)
               OR (v_row ? 'track_id' AND v_row ->> 'track_id' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'track_id') > 64)
               OR (v_row ? 'temporal_role' AND v_row ->> 'temporal_role' IS NOT NULL AND v_row ->> 'temporal_role' NOT IN ('keyframe','derived','sample')) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E fixture Annotation value' USING ERRCODE = '22023';
            END IF;
        END LOOP;
        v_pending := p_request -> 'rows';
        v_remaining := pg_catalog.jsonb_array_length(v_pending);
        WHILE v_remaining > 0 LOOP
            v_progress := false;
            FOR v_index IN REVERSE v_remaining - 1..0 LOOP
                v_row := v_pending -> v_index;
                v_parent_id := NULL;
                IF v_row ? 'parent_annotation_id'
                   AND v_row -> 'parent_annotation_id' <> 'null'::jsonb THEN
                    IF pg_catalog.jsonb_typeof(v_row -> 'parent_annotation_id') IS DISTINCT FROM 'string'
                       OR (v_row ->> 'parent_annotation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                        RAISE EXCEPTION 'Invalid Labeling E2E Annotation parent' USING ERRCODE = '22023';
                    END IF;
                    v_parent_id := (v_row ->> 'parent_annotation_id')::uuid;
                END IF;
                IF v_parent_id IS NULL OR EXISTS (
                    SELECT FROM public.annotations AS parent_annotation
                    WHERE parent_annotation.id=v_parent_id
                ) THEN
                    SELECT task.project_id INTO v_task_project_id
                    FROM public.tasks AS task WHERE task.id=(v_row ->> 'task_id')::uuid;
                    IF NOT FOUND OR v_task_project_id IS DISTINCT FROM v_project_id THEN
                        RAISE EXCEPTION USING ERRCODE = '23503',
                            MESSAGE = 'Labeling E2E fixture Annotation Task is missing';
                    END IF;
                    INSERT INTO public.annotations (
                        id, task_id, project_id, user_id, source, annotation_type,
                        tool_unit_id, class_name, geometry, confidence,
                        parent_prediction_id, parent_annotation_id, track_id,
                        scene_track_id, temporal_role, lead_time, was_cancelled,
                        ground_truth, is_active, attributes, is_locked,
                        created_at, updated_at, attributes_meta, z_order,
                        is_hidden, version
                    ) VALUES (
                        (v_row ->> 'id')::uuid, (v_row ->> 'task_id')::uuid,
                        v_project_id,
                        CASE WHEN v_row ? 'user_id' THEN (v_row ->> 'user_id')::uuid ELSE NULL END,
                        CASE WHEN v_row ? 'source' THEN v_row ->> 'source' ELSE 'manual' END,
                        CASE WHEN v_row ? 'annotation_type' THEN v_row ->> 'annotation_type' ELSE 'bbox' END,
                        CASE WHEN v_row ? 'tool_unit_id' THEN v_row ->> 'tool_unit_id' ELSE 'bbox' END,
                        v_row ->> 'class_name', v_row -> 'geometry',
                        CASE WHEN v_row ? 'confidence' THEN (v_row ->> 'confidence')::double precision ELSE NULL END,
                        CASE WHEN v_row ? 'parent_prediction_id' THEN (v_row ->> 'parent_prediction_id')::uuid ELSE NULL END,
                        v_parent_id,
                        CASE WHEN v_row ? 'track_id' THEN v_row ->> 'track_id' ELSE NULL END,
                        CASE WHEN v_row ? 'scene_track_id' THEN (v_row ->> 'scene_track_id')::uuid ELSE NULL END,
                        CASE WHEN v_row ? 'temporal_role' THEN v_row ->> 'temporal_role' ELSE 'sample' END,
                        CASE WHEN v_row ? 'lead_time' THEN (v_row ->> 'lead_time')::double precision ELSE NULL END,
                        CASE WHEN v_row ? 'was_cancelled' THEN (v_row ->> 'was_cancelled')::boolean ELSE false END,
                        CASE WHEN v_row ? 'ground_truth' THEN (v_row ->> 'ground_truth')::boolean ELSE false END,
                        CASE WHEN v_row ? 'is_active' THEN (v_row ->> 'is_active')::boolean ELSE true END,
                        CASE WHEN v_row ? 'attributes' THEN v_row -> 'attributes' ELSE '{}'::jsonb END,
                        CASE WHEN v_row ? 'is_locked' THEN (v_row ->> 'is_locked')::boolean ELSE false END,
                        CASE WHEN v_row ? 'created_at' THEN (v_row ->> 'created_at')::timestamptz ELSE pg_catalog.transaction_timestamp() END,
                        CASE WHEN v_row ? 'updated_at' THEN (v_row ->> 'updated_at')::timestamptz ELSE pg_catalog.transaction_timestamp() END,
                        '{}'::jsonb, 0, false, 1
                    );
                    v_row_id := (v_row ->> 'id')::uuid;
                    v_ids := v_ids || pg_catalog.jsonb_build_array(v_row_id);
                    v_pending := v_pending - v_index;
                    v_remaining := v_remaining - 1;
                    v_progress := true;
                END IF;
            END LOOP;
            IF NOT v_progress THEN
                RAISE EXCEPTION USING ERRCODE = '23503',
                    MESSAGE = 'Labeling E2E fixture Annotation parent cycle or missing row';
            END IF;
        END LOOP;
        SELECT COALESCE(pg_catalog.jsonb_agg((value ->> 'id')::uuid ORDER BY ordinality), '[]'::jsonb)
        INTO v_ids
        FROM pg_catalog.jsonb_array_elements(p_request -> 'rows') WITH ORDINALITY AS input(value, ordinality);

    ELSIF v_operation = 'insert_video_tracker_jobs' THEN
        IF (p_request - ARRAY['project_id','operation','rows','schema']) <> '{}'::jsonb
           OR NOT (p_request ?& ARRAY['project_id','operation','rows','schema'])
           OR pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR (p_request ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'rows') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') < 1
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') > 512 THEN
            RAISE EXCEPTION 'Invalid Labeling E2E tracker Job request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        FOR v_row IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'rows') AS rows(value) LOOP
            IF pg_catalog.jsonb_typeof(v_row) IS DISTINCT FROM 'object'
               OR (v_row - ARRAY[
                    'id','task_id','dataset_item_id','annotation_id','segment_id','created_by',
                    'status','job_kind','model_key','direction','from_frame','to_frame',
                    'prompt','staged_result','revision','event_channel','celery_task_id',
                    'started_at','completed_at','error_message'
               ]) <> '{}'::jsonb
               OR NOT (v_row ?& ARRAY['id','task_id','dataset_item_id','model_key','direction','from_frame','to_frame','event_channel'])
               OR pg_catalog.jsonb_typeof(v_row -> 'id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'task_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'dataset_item_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'model_key') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'direction') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'from_frame') IS DISTINCT FROM 'number'
               OR pg_catalog.jsonb_typeof(v_row -> 'to_frame') IS DISTINCT FROM 'number'
               OR pg_catalog.jsonb_typeof(v_row -> 'event_channel') IS DISTINCT FROM 'string'
               OR (v_row ->> 'id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'task_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'dataset_item_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'direction') NOT IN ('forward','backward','bidirectional')
               OR pg_catalog.char_length(v_row ->> 'event_channel') NOT BETWEEN 1 AND 160
               OR pg_catalog.char_length(v_row ->> 'model_key') NOT BETWEEN 1 AND 80
               OR pg_catalog.char_length(v_row ->> 'direction') > 20
               OR (v_row ->> 'from_frame') !~ '^(0|[1-9][0-9]*)$'
               OR (v_row ->> 'to_frame') !~ '^(0|[1-9][0-9]*)$'
               OR (v_row ->> 'from_frame')::numeric > 2147483647
               OR (v_row ->> 'to_frame')::numeric > 2147483647
               OR (v_row ->> 'from_frame')::integer > (v_row ->> 'to_frame')::integer THEN
                RAISE EXCEPTION 'Invalid Labeling E2E tracker Job row' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'annotation_id' AND pg_catalog.jsonb_typeof(v_row -> 'annotation_id') NOT IN ('string','null'))
               OR (v_row ? 'segment_id' AND pg_catalog.jsonb_typeof(v_row -> 'segment_id') NOT IN ('string','null'))
               OR (v_row ? 'created_by' AND pg_catalog.jsonb_typeof(v_row -> 'created_by') NOT IN ('string','null'))
               OR (v_row ? 'status' AND pg_catalog.jsonb_typeof(v_row -> 'status') NOT IN ('string','null'))
               OR (v_row ? 'job_kind' AND pg_catalog.jsonb_typeof(v_row -> 'job_kind') NOT IN ('string','null'))
               OR (v_row ? 'prompt' AND pg_catalog.jsonb_typeof(v_row -> 'prompt') NOT IN ('object','null'))
               OR (v_row ? 'staged_result' AND pg_catalog.jsonb_typeof(v_row -> 'staged_result') NOT IN ('object','null'))
               OR (v_row ? 'revision' AND pg_catalog.jsonb_typeof(v_row -> 'revision') NOT IN ('number','null'))
               OR (v_row ? 'celery_task_id' AND pg_catalog.jsonb_typeof(v_row -> 'celery_task_id') NOT IN ('string','null'))
               OR (v_row ? 'started_at' AND pg_catalog.jsonb_typeof(v_row -> 'started_at') NOT IN ('string','null'))
               OR (v_row ? 'completed_at' AND pg_catalog.jsonb_typeof(v_row -> 'completed_at') NOT IN ('string','null'))
               OR (v_row ? 'error_message' AND pg_catalog.jsonb_typeof(v_row -> 'error_message') NOT IN ('string','null')) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E tracker Job field types' USING ERRCODE = '22023';
            END IF;
            IF (v_row ? 'annotation_id' AND pg_catalog.jsonb_typeof(v_row -> 'annotation_id') = 'string'
                AND (v_row ->> 'annotation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'segment_id' AND pg_catalog.jsonb_typeof(v_row -> 'segment_id') = 'string'
                AND (v_row ->> 'segment_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'created_by' AND pg_catalog.jsonb_typeof(v_row -> 'created_by') = 'string'
                AND (v_row ->> 'created_by') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR (v_row ? 'status' AND v_row ->> 'status' IS NOT NULL AND v_row ->> 'status' <> 'pending_review')
               OR (v_row ? 'job_kind' AND v_row ->> 'job_kind' IS NOT NULL AND v_row ->> 'job_kind' <> 'tracking')
               OR (v_row ? 'revision' AND pg_catalog.jsonb_typeof(v_row -> 'revision') = 'number'
                   AND ((v_row ->> 'revision') !~ '^[1-9][0-9]*$' OR (v_row ->> 'revision')::numeric > 2147483647))
               OR (v_row ? 'celery_task_id' AND v_row ->> 'celery_task_id' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'celery_task_id') > 64) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E tracker Job value' USING ERRCODE = '22023';
            END IF;
            IF EXISTS (
                SELECT FROM public.tasks AS task
                WHERE task.id=(v_row ->> 'task_id')::uuid
                  AND task.project_id=v_project_id
                  AND task.dataset_item_id IS DISTINCT FROM (v_row ->> 'dataset_item_id')::uuid
            ) THEN
                RAISE EXCEPTION 'Tracker Job fixture Task item changed' USING ERRCODE = '23514';
            END IF;
            IF NOT EXISTS (SELECT FROM public.tasks AS task WHERE task.id=(v_row ->> 'task_id')::uuid AND task.project_id=v_project_id) THEN
                RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'Labeling E2E tracker Job Task is missing';
            END IF;
            INSERT INTO public.video_tracker_jobs (
                id, task_id, dataset_item_id, annotation_id, segment_id, created_by,
                status, job_kind, model_key, direction, from_frame, to_frame,
                prompt, staged_result, revision, event_channel, celery_task_id,
                started_at, completed_at, error_message, created_at, updated_at
            ) VALUES (
                (v_row ->> 'id')::uuid, (v_row ->> 'task_id')::uuid,
                (v_row ->> 'dataset_item_id')::uuid,
                CASE WHEN v_row ? 'annotation_id' THEN (v_row ->> 'annotation_id')::uuid ELSE NULL END,
                CASE WHEN v_row ? 'segment_id' THEN (v_row ->> 'segment_id')::uuid ELSE NULL END,
                CASE WHEN v_row ? 'created_by' THEN (v_row ->> 'created_by')::uuid ELSE NULL END,
                CASE WHEN v_row ? 'status' THEN v_row ->> 'status' ELSE 'queued' END,
                CASE WHEN v_row ? 'job_kind' THEN v_row ->> 'job_kind' ELSE 'tracking' END,
                v_row ->> 'model_key', v_row ->> 'direction',
                (v_row ->> 'from_frame')::integer, (v_row ->> 'to_frame')::integer,
                CASE WHEN v_row ? 'prompt' THEN v_row -> 'prompt' ELSE '{}'::jsonb END,
                CASE WHEN v_row ? 'staged_result' THEN v_row -> 'staged_result' ELSE NULL END,
                CASE WHEN v_row ? 'revision' THEN (v_row ->> 'revision')::integer ELSE 1 END,
                v_row ->> 'event_channel',
                CASE WHEN v_row ? 'celery_task_id' THEN v_row ->> 'celery_task_id' ELSE NULL END,
                CASE WHEN v_row ? 'started_at' THEN (v_row ->> 'started_at')::timestamptz ELSE NULL END,
                CASE WHEN v_row ? 'completed_at' THEN (v_row ->> 'completed_at')::timestamptz ELSE NULL END,
                CASE WHEN v_row ? 'error_message' THEN v_row ->> 'error_message' ELSE NULL END,
                pg_catalog.transaction_timestamp(), pg_catalog.transaction_timestamp()
            ) RETURNING id INTO v_row_id;
            v_ids := v_ids || pg_catalog.jsonb_build_array(v_row_id);
        END LOOP;

    ELSIF v_operation = 'insert_task_dataset_item_links' THEN
        IF (p_request - ARRAY['project_id','task_id','operation','rows','schema']) <> '{}'::jsonb
           OR NOT (p_request ?& ARRAY['project_id','task_id','operation','rows','schema'])
           OR pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
           OR (p_request ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR (p_request ->> 'task_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'rows') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') < 1
           OR pg_catalog.jsonb_array_length(p_request -> 'rows') > 512 THEN
            RAISE EXCEPTION 'Invalid Labeling E2E TaskDatasetItemLink request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_task_id := (p_request ->> 'task_id')::uuid;
        IF NOT EXISTS (SELECT FROM public.tasks AS task WHERE task.id=v_task_id AND task.project_id=v_project_id) THEN
            RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'Labeling E2E TaskDatasetItemLink Task is missing';
        END IF;
        FOR v_row IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'rows') AS rows(value) LOOP
            IF pg_catalog.jsonb_typeof(v_row) IS DISTINCT FROM 'object'
               OR (v_row - ARRAY['id','task_id','dataset_item_id','role','sensor_name']) <> '{}'::jsonb
               OR NOT (v_row ?& ARRAY['id','task_id','dataset_item_id','role'])
               OR pg_catalog.jsonb_typeof(v_row -> 'id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'task_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'dataset_item_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_row -> 'role') IS DISTINCT FROM 'string'
               OR (v_row ->> 'id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'task_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row ->> 'task_id')::uuid IS DISTINCT FROM v_task_id
               OR (v_row ->> 'dataset_item_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.char_length(v_row ->> 'role') NOT BETWEEN 1 AND 50
               OR ((v_row ->> 'role') <> 'primary_lidar' AND (v_row ->> 'role') NOT LIKE 'camera\_%' ESCAPE '\')
               OR (v_row ? 'sensor_name' AND pg_catalog.jsonb_typeof(v_row -> 'sensor_name') NOT IN ('string','null'))
               OR (v_row ? 'sensor_name' AND v_row ->> 'sensor_name' IS NOT NULL AND pg_catalog.char_length(v_row ->> 'sensor_name') > 100) THEN
                RAISE EXCEPTION 'Invalid Labeling E2E TaskDatasetItemLink row' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.task_dataset_item_links (
                id, task_id, dataset_item_id, role, sensor_name, created_at
            ) VALUES (
                (v_row ->> 'id')::uuid, v_task_id,
                (v_row ->> 'dataset_item_id')::uuid, v_row ->> 'role',
                CASE WHEN v_row ? 'sensor_name' THEN v_row ->> 'sensor_name' ELSE NULL END,
                pg_catalog.transaction_timestamp()
            ) RETURNING id INTO v_row_id;
            v_ids := v_ids || pg_catalog.jsonb_build_array(v_row_id);
        END LOOP;

    ELSIF v_operation = 'patch_feedback_anchor' THEN
        IF (p_request - ARRAY['project_id','task_id','feedback_id','anchor_position','operation','schema']) <> '{}'::jsonb
           OR NOT (p_request ?& ARRAY['project_id','task_id','feedback_id','anchor_position','operation','schema'])
           OR pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'feedback_id') IS DISTINCT FROM 'string'
           OR (p_request ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR (p_request ->> 'task_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR (p_request ->> 'feedback_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'anchor_position') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid Labeling E2E feedback-anchor request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_feedback_id := (p_request ->> 'feedback_id')::uuid;
        IF NOT EXISTS (
            SELECT FROM public.tasks AS task
            WHERE task.id=v_task_id AND task.project_id=v_project_id
              AND task.file_type='video' AND task.display_id LIKE 'T-E2E-%'
        ) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_labeling_e2e_fixture_error',
                DETAIL = pg_catalog.jsonb_build_object('reason','task_target_changed')::text;
        END IF;
        SELECT feedback.anchor_position INTO v_existing_anchor
        FROM public.annotation_feedbacks AS feedback
        WHERE feedback.id=v_feedback_id
          AND feedback.project_id=v_project_id
          AND feedback.task_id=v_task_id
          AND feedback.kind='issue'
          AND feedback.anchor_type='pixel'
          AND feedback.is_active IS TRUE
        FOR UPDATE;
        IF NOT FOUND
           OR pg_catalog.jsonb_typeof(v_existing_anchor) IS DISTINCT FROM 'object'
           OR NOT (v_existing_anchor ?& ARRAY['x','y','frame']) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_labeling_e2e_fixture_error',
                DETAIL = pg_catalog.jsonb_build_object('reason','feedback_target_changed')::text;
        END IF;
        UPDATE public.annotation_feedbacks AS feedback
        SET anchor_position=p_request -> 'anchor_position',
            updated_at=pg_catalog.transaction_timestamp()
        WHERE feedback.id=v_feedback_id
          AND feedback.project_id=v_project_id
          AND feedback.task_id=v_task_id;
        v_ids := pg_catalog.jsonb_build_array(v_feedback_id);
        v_anchor_position := p_request -> 'anchor_position';
    ELSE
        RAISE EXCEPTION 'Unsupported Labeling E2E fixture operation' USING ERRCODE = '22023';
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema','aap-labeling-e2e-fixtures/v1',
        'operation',v_operation,
        'ok',true,
        'data',pg_catalog.jsonb_build_object(
            'ids',v_ids,
            'anchor_position',v_anchor_position
        )
    );
EXCEPTION
    WHEN invalid_text_representation OR invalid_datetime_format
         OR datetime_field_overflow OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'Invalid Labeling E2E fixture value' USING ERRCODE = '22023';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.e2e_fixture_command(jsonb) FROM PUBLIC;
