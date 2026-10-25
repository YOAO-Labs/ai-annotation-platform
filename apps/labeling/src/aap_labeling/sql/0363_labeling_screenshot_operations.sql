-- Screenshot-only operational writes stay inside Labeling's bounded owner.
CREATE FUNCTION aap_labeling_v1.screenshot_operations_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_project_id uuid;
    v_task_id uuid;
    v_task_ids uuid[];
    v_project_display_id text;
    v_project_key text;
    v_project_json jsonb;
    v_expected_count integer;
    v_entry jsonb;
    v_id uuid;
    v_batch_id uuid;
    v_prediction_id uuid;
    v_prediction_created_at timestamptz;
    v_expected_task_display_id text;
    v_expected_annotation_time timestamptz;
    v_expected_annotation_geometry jsonb;
    v_expected_annotation_ground_truth boolean;
    v_expected_batch_display_id text;
    v_task_display_id text;
    v_task_reviewed_at timestamptz;
    v_task_submitted_at timestamptz;
    v_task_assigned_at timestamptz;
    v_task_reviewer_id uuid;
    v_expected_status text;
    v_task_sequence integer;
    v_expected_task_created_at timestamptz;
    v_expected_task_updated_at timestamptz;
    v_fixed_time timestamptz;
    v_count bigint := 0;
    v_rows bigint := 0;
    v_total bigint := 0;
    v_predictions bigint := 0;
    v_annotations bigint := 0;
    v_tracker_jobs bigint := 0;
    v_failed_predictions bigint := 0;
    v_feedbacks bigint := 0;
    v_changed boolean := false;
    v_data jsonb;
BEGIN
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR pg_catalog.octet_length(p_request::text) > 8388608
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-screenshot-operations/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid screenshot operations request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';

    IF v_operation = 'reset_seed_tasks' THEN
        IF (p_request - ARRAY['schema','operation','project_id','project_display_id','tasks']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','project_id','project_display_id','tasks'])
           OR pg_catalog.jsonb_typeof(p_request -> 'tasks') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'tasks') < 1 THEN
            RAISE EXCEPTION 'Invalid screenshot task reset request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_project_display_id := p_request ->> 'project_display_id';
        IF v_project_display_id NOT IN ('P-COCO8','P-VIDEO-DEV','P-PC-DEV','P-PC-MULTI','P-OCR') THEN
            RAISE EXCEPTION 'Screenshot project scope changed' USING ERRCODE = '23514';
        END IF;
        v_project_json := aap_workspace_v1.read_project_row(v_project_id)::jsonb;
        IF v_project_json IS NULL OR v_project_json ->> 'display_id' IS DISTINCT FROM v_project_display_id THEN
            RAISE EXCEPTION 'Screenshot project scope changed' USING ERRCODE = '23514';
        END IF;
        v_expected_count := CASE v_project_display_id
            WHEN 'P-COCO8' THEN 8 WHEN 'P-VIDEO-DEV' THEN 1
            WHEN 'P-PC-DEV' THEN 39 WHEN 'P-PC-MULTI' THEN 1 WHEN 'P-OCR' THEN 1
        END;
        SELECT pg_catalog.array_agg((entry ->> 'id')::uuid ORDER BY (entry ->> 'id')::uuid)
        INTO v_task_ids FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS entry;
        IF v_task_ids IS NULL
           OR pg_catalog.cardinality(v_task_ids) <> v_expected_count
           OR pg_catalog.cardinality(v_task_ids) <> (SELECT pg_catalog.count(DISTINCT value) FROM pg_catalog.unnest(v_task_ids) AS value)
           OR pg_catalog.jsonb_array_length(p_request -> 'tasks') <> (SELECT pg_catalog.count(DISTINCT entry ->> 'display_id') FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS entry)
           OR pg_catalog.jsonb_array_length(p_request -> 'tasks') <> (SELECT pg_catalog.count(DISTINCT entry ->> 'sequence_order') FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS entry)
           OR EXISTS (SELECT FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS entry
                      WHERE (entry - ARRAY['id','file_path','display_id','sequence_order','status']) IS DISTINCT FROM '{}'::jsonb
                         OR NOT (entry ?& ARRAY['id','file_path','display_id','sequence_order','status'])
                         OR pg_catalog.jsonb_typeof(entry -> 'id') IS DISTINCT FROM 'string'
                         OR pg_catalog.jsonb_typeof(entry -> 'file_path') IS DISTINCT FROM 'string'
                         OR pg_catalog.jsonb_typeof(entry -> 'display_id') IS DISTINCT FROM 'string'
                         OR pg_catalog.jsonb_typeof(entry -> 'sequence_order') IS DISTINCT FROM 'number'
                         OR (entry ->> 'sequence_order') !~ '^(0|[1-9][0-9]*)$'
                         OR (entry ->> 'sequence_order')::integer >= v_expected_count
                         OR pg_catalog.substring(entry ->> 'display_id', '([0-9]{3})$')::integer
                            <> (entry ->> 'sequence_order')::integer+1
                         OR pg_catalog.jsonb_typeof(entry -> 'status') IS DISTINCT FROM 'string'
                         OR (entry ->> 'status') NOT IN ('pending','in_progress','review','completed')
                         OR (entry ->> 'display_id') !~ ('^T-SS-' || pg_catalog.substring(v_project_display_id, 3) || '-[0-9]{3}$')
                         OR NOT CASE v_project_display_id
                             WHEN 'P-COCO8' THEN (entry ->> 'file_path') IN (
                                 'coco8-dev/train/screenshot_01.jpg','coco8-dev/train/screenshot_02.jpg',
                                 'coco8-dev/train/screenshot_03.jpg','coco8-dev/train/screenshot_04.jpg',
                                 'coco8-dev/val/screenshot_05.jpg','coco8-dev/val/screenshot_06.jpg',
                                 'coco8-dev/val/screenshot_07.jpg','coco8-dev/val/screenshot_08.jpg')
                             WHEN 'P-VIDEO-DEV' THEN (entry ->> 'file_path')='tracking-car-dev/tracking_scene.mp4'
                             WHEN 'P-PC-DEV' THEN (entry ->> 'file_path') ~ '^(nuscenes-mini|__aap_trusted_nuscenes__-[0-9a-f]{32})/scene-0061/lidar/scene-0061_[0-9]{6}[.]pcd$'
                                 AND pg_catalog.substring(entry ->> 'file_path', '([0-9]{6})[.]pcd$')::integer < 39
                             WHEN 'P-PC-MULTI' THEN (entry ->> 'file_path')='pc-multicam-dev/lidar/000000.pcd'
                             WHEN 'P-OCR' THEN (entry ->> 'file_path')='ocr-dev/ch_en_num.jpg'
                             ELSE false
                         END) THEN
            RAISE EXCEPTION 'Invalid screenshot task reset scope' USING ERRCODE = '22023';
        END IF;
        IF EXISTS (SELECT FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS entry
                   WHERE (entry ->> 'status') IS DISTINCT FROM CASE
                       WHEN v_project_display_id='P-COCO8' AND (entry ->> 'sequence_order')::integer=2 THEN 'in_progress'
                       WHEN v_project_display_id='P-COCO8' AND (entry ->> 'sequence_order')::integer IN (3,4) THEN 'review'
                       WHEN v_project_display_id='P-COCO8' AND (entry ->> 'sequence_order')::integer=5 THEN 'completed'
                       ELSE 'pending' END) THEN
            RAISE EXCEPTION 'Invalid screenshot task initial status' USING ERRCODE = '22023';
        END IF;
        FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS rows(value)
        LOOP
            v_id := (v_entry ->> 'id')::uuid;
            UPDATE public.tasks AS task
            SET display_id=v_entry ->> 'display_id',
                sequence_order=(v_entry ->> 'sequence_order')::integer,
                status=v_entry ->> 'status',
                assignee_id=NULL, reviewer_id=NULL, batch_id=NULL,
                assigned_at=NULL, submitted_at=NULL, reviewer_claimed_at=NULL,
                reviewed_at=NULL, reject_reason=NULL, reject_reason_type=NULL,
                skip_reason=NULL, skipped_at=NULL, total_annotations=0,
                total_predictions=0, is_labeled=false
            WHERE task.id=v_id AND task.project_id=v_project_id
              AND task.file_path=v_entry ->> 'file_path';
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                RAISE EXCEPTION 'Screenshot task scope changed' USING ERRCODE = '23514';
            END IF;
            v_count := v_count + v_rows;
        END LOOP;
        v_changed := v_count > 0;
        v_data := pg_catalog.jsonb_build_object('tasks',v_count);

    ELSIF v_operation = 'reconcile_seed_batches' THEN
        IF (p_request - ARRAY['schema','operation','project_id','dataset_id','created_by','fixed_time','batches']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','project_id','dataset_id','created_by','fixed_time','batches'])
           OR pg_catalog.jsonb_typeof(p_request -> 'batches') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'batches') <> 5
           OR pg_catalog.jsonb_typeof(p_request -> 'fixed_time') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid screenshot batch reconciliation request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_fixed_time := (p_request ->> 'fixed_time')::timestamptz;
        v_project_json := aap_workspace_v1.read_project_row(v_project_id)::jsonb;
        IF v_project_json IS NULL OR v_project_json ->> 'display_id' IS DISTINCT FROM 'P-COCO8' THEN
            RAISE EXCEPTION 'Screenshot batch project scope changed' USING ERRCODE = '23514';
        END IF;
        IF v_fixed_time IS DISTINCT FROM TIMESTAMPTZ '2026-07-01 09:00:00+00' THEN
            RAISE EXCEPTION 'Invalid screenshot batch timestamp' USING ERRCODE = '22023';
        END IF;
        IF EXISTS (SELECT FROM pg_catalog.jsonb_array_elements(p_request -> 'batches') AS entry
                   WHERE (entry - ARRAY['id','existing','display_id','name','status','annotator_id','reviewer_id','assigned_user_ids']) IS DISTINCT FROM '{}'::jsonb
                      OR NOT (entry ?& ARRAY['id','existing','display_id','name','status','annotator_id','reviewer_id','assigned_user_ids'])
                      OR pg_catalog.jsonb_typeof(entry -> 'id') IS DISTINCT FROM 'string'
                      OR pg_catalog.jsonb_typeof(entry -> 'existing') IS DISTINCT FROM 'boolean'
                      OR pg_catalog.jsonb_typeof(entry -> 'display_id') IS DISTINCT FROM 'string'
                      OR pg_catalog.jsonb_typeof(entry -> 'name') IS DISTINCT FROM 'string'
                      OR pg_catalog.jsonb_typeof(entry -> 'status') IS DISTINCT FROM 'string'
                      OR pg_catalog.jsonb_typeof(entry -> 'assigned_user_ids') IS DISTINCT FROM 'array'
                      OR (entry ->> 'display_id',entry ->> 'status',entry ->> 'name') NOT IN (
                          ('B-SS-DRAFT','draft','截图 · 待分派'),
                          ('B-SS-ACTIVE','active','截图 · 待预标'),
                          ('B-SS-ANNOTATING','annotating','截图 · 标注中'),
                          ('B-SS-REVIEW','reviewing','截图 · 待审核'),
                          ('B-SS-COMPLETE','approved','截图 · 已完成')
                      ))
           OR (SELECT pg_catalog.count(DISTINCT entry ->> 'display_id') FROM pg_catalog.jsonb_array_elements(p_request -> 'batches') AS entry) <> 5 THEN
            RAISE EXCEPTION 'Invalid screenshot batch scope' USING ERRCODE = '22023';
        END IF;
        FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'batches') AS rows(value)
        LOOP
            v_batch_id := (v_entry ->> 'id')::uuid;
            IF (v_entry ->> 'existing')::boolean THEN
                UPDATE public.task_batches AS batch
                SET dataset_id=(p_request ->> 'dataset_id')::uuid,
                    name=v_entry ->> 'name', description='screenshots seed managed batch',
                    status=v_entry ->> 'status', priority=50,
                    annotator_id=NULLIF(v_entry ->> 'annotator_id','null')::uuid,
                    reviewer_id=NULLIF(v_entry ->> 'reviewer_id','null')::uuid,
                    assigned_user_ids=v_entry -> 'assigned_user_ids',
                    created_by=(p_request ->> 'created_by')::uuid,
                    created_at=v_fixed_time, updated_at=v_fixed_time
                WHERE batch.id=v_batch_id AND batch.project_id=v_project_id
                  AND batch.display_id=v_entry ->> 'display_id';
                GET DIAGNOSTICS v_rows = ROW_COUNT;
                IF v_rows <> 1 THEN
                    RAISE EXCEPTION 'Screenshot batch scope changed' USING ERRCODE = '23514';
                END IF;
            ELSE
                INSERT INTO public.task_batches (
                    id,project_id,dataset_id,display_id,name,description,status,priority,
                    annotator_id,reviewer_id,assigned_user_ids,created_by,created_at,updated_at
                ) VALUES (
                    v_batch_id,v_project_id,(p_request ->> 'dataset_id')::uuid,
                    v_entry ->> 'display_id',v_entry ->> 'name',
                    'screenshots seed managed batch',v_entry ->> 'status',50,
                    NULLIF(v_entry ->> 'annotator_id','null')::uuid,
                    NULLIF(v_entry ->> 'reviewer_id','null')::uuid,
                    v_entry -> 'assigned_user_ids',(p_request ->> 'created_by')::uuid,
                    v_fixed_time,v_fixed_time
                );
                GET DIAGNOSTICS v_rows = ROW_COUNT;
            END IF;
            v_count := v_count + v_rows;
        END LOOP;
        v_changed := v_count > 0;
        v_data := pg_catalog.jsonb_build_object('rows',v_count);

    ELSIF v_operation = 'link_seed_tasks_to_batches' THEN
        IF (p_request - ARRAY['schema','operation','project_id','tasks']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','project_id','tasks'])
           OR pg_catalog.jsonb_typeof(p_request -> 'tasks') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'tasks') <> 8
           OR pg_catalog.jsonb_array_length(p_request -> 'tasks') <>
              (SELECT pg_catalog.count(DISTINCT entry ->> 'task_id') FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS entry) THEN
            RAISE EXCEPTION 'Invalid screenshot task batch link request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_project_json := aap_workspace_v1.read_project_row(v_project_id)::jsonb;
        IF v_project_json IS NULL OR v_project_json ->> 'display_id' IS DISTINCT FROM 'P-COCO8' THEN
            RAISE EXCEPTION 'Screenshot task batch project changed' USING ERRCODE = '23514';
        END IF;
        FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS rows(value)
        LOOP
            IF (v_entry - ARRAY['task_id','batch_id']) IS DISTINCT FROM '{}'::jsonb
               OR NOT (v_entry ?& ARRAY['task_id','batch_id']) THEN
                RAISE EXCEPTION 'Invalid screenshot task batch link fields' USING ERRCODE = '22023';
            END IF;
            v_batch_id := (v_entry ->> 'batch_id')::uuid;
            SELECT task.display_id INTO v_task_display_id FROM public.tasks AS task
            WHERE task.id=(v_entry ->> 'task_id')::uuid AND task.project_id=v_project_id
              AND task.display_id IN ('T-SS-COCO8-001','T-SS-COCO8-002','T-SS-COCO8-003',
                  'T-SS-COCO8-004','T-SS-COCO8-005','T-SS-COCO8-006','T-SS-COCO8-007','T-SS-COCO8-008');
            v_expected_batch_display_id := CASE v_task_display_id
                WHEN 'T-SS-COCO8-001' THEN 'B-SS-DRAFT'
                WHEN 'T-SS-COCO8-002' THEN 'B-SS-DRAFT'
                WHEN 'T-SS-COCO8-003' THEN 'B-SS-ANNOTATING'
                WHEN 'T-SS-COCO8-004' THEN 'B-SS-REVIEW'
                WHEN 'T-SS-COCO8-005' THEN 'B-SS-REVIEW'
                WHEN 'T-SS-COCO8-006' THEN 'B-SS-COMPLETE'
                WHEN 'T-SS-COCO8-007' THEN 'B-SS-ACTIVE'
                WHEN 'T-SS-COCO8-008' THEN 'B-SS-DRAFT'
            END;
            IF v_expected_batch_display_id IS NULL
               OR NOT EXISTS (SELECT FROM public.task_batches AS batch WHERE batch.id=v_batch_id
                              AND batch.project_id=v_project_id
                              AND batch.display_id=v_expected_batch_display_id) THEN
                RAISE EXCEPTION 'Screenshot task batch target changed' USING ERRCODE = '23514';
            END IF;
            UPDATE public.tasks AS task SET batch_id=v_batch_id
            WHERE task.id=(v_entry ->> 'task_id')::uuid AND task.project_id=v_project_id
              AND task.display_id=v_task_display_id;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                RAISE EXCEPTION 'Screenshot task batch scope changed' USING ERRCODE = '23514';
            END IF;
            v_count := v_count + v_rows;
        END LOOP;
        v_changed := v_count > 0;
        v_data := pg_catalog.jsonb_build_object('tasks',v_count);

    ELSIF v_operation = 'set_image_seed_task_state' THEN
        IF (p_request - ARRAY['schema','operation','project_id','tasks']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','project_id','tasks'])
           OR pg_catalog.jsonb_typeof(p_request -> 'tasks') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'tasks') <> 8 THEN
            RAISE EXCEPTION 'Invalid screenshot image task state request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_project_json := aap_workspace_v1.read_project_row(v_project_id)::jsonb;
        IF v_project_json IS NULL OR v_project_json ->> 'display_id' IS DISTINCT FROM 'P-COCO8' THEN
            RAISE EXCEPTION 'Screenshot image project scope changed' USING ERRCODE = '23514';
        END IF;
        v_fixed_time := TIMESTAMPTZ '2026-07-01 09:00:00+00';
        IF pg_catalog.jsonb_array_length(p_request -> 'tasks') <>
           (SELECT pg_catalog.count(DISTINCT entry ->> 'task_id') FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS entry)
           OR pg_catalog.jsonb_array_length(p_request -> 'tasks') <>
              (SELECT pg_catalog.count(DISTINCT entry ->> 'sequence_order') FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS entry) THEN
            RAISE EXCEPTION 'Invalid screenshot image task set' USING ERRCODE = '22023';
        END IF;
        FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS rows(value)
        LOOP
            IF (v_entry - ARRAY['task_id','status','sequence_order','assignee_id','reviewer_id','assigned_at','submitted_at','reviewer_claimed_at','reviewed_at']) IS DISTINCT FROM '{}'::jsonb
               OR NOT (v_entry ?& ARRAY['task_id','status','sequence_order','assignee_id','reviewer_id','assigned_at','submitted_at','reviewer_claimed_at','reviewed_at'])
               OR (v_entry ->> 'status') NOT IN ('pending','in_progress','review','completed') THEN
                RAISE EXCEPTION 'Invalid screenshot image task state fields' USING ERRCODE = '22023';
            END IF;
            v_expected_status := CASE (v_entry ->> 'sequence_order')::integer
                WHEN 2 THEN 'in_progress' WHEN 3 THEN 'review' WHEN 4 THEN 'review'
                WHEN 5 THEN 'completed' ELSE 'pending' END;
            IF (v_entry ->> 'status') IS DISTINCT FROM v_expected_status THEN
                RAISE EXCEPTION 'Invalid screenshot image task status' USING ERRCODE = '22023';
            END IF;
            IF ((v_entry ->> 'assignee_id') IS NOT NULL) IS DISTINCT FROM
                   ((v_entry ->> 'sequence_order')::integer BETWEEN 2 AND 5)
               OR ((v_entry ->> 'reviewer_id') IS NOT NULL) IS DISTINCT FROM
                   ((v_entry ->> 'sequence_order')::integer IN (4,5))
               OR (v_entry ->> 'assigned_at')::timestamptz IS DISTINCT FROM
                   (CASE WHEN (v_entry ->> 'sequence_order')::integer BETWEEN 2 AND 5
                        THEN v_fixed_time + INTERVAL '1 hour' ELSE NULL END)
               OR (v_entry ->> 'submitted_at')::timestamptz IS DISTINCT FROM
                   (CASE WHEN (v_entry ->> 'status') IN ('review','completed')
                        THEN v_fixed_time + INTERVAL '4 hours' ELSE NULL END)
               OR (v_entry ->> 'reviewer_claimed_at')::timestamptz IS DISTINCT FROM
                   (CASE WHEN (v_entry ->> 'sequence_order')::integer IN (4,5)
                        THEN v_fixed_time + INTERVAL '4 hours 20 minutes' ELSE NULL END)
               OR (v_entry ->> 'reviewed_at')::timestamptz IS DISTINCT FROM
                   (CASE WHEN (v_entry ->> 'status')='completed'
                        THEN v_fixed_time + INTERVAL '6 hours' ELSE NULL END) THEN
                RAISE EXCEPTION 'Invalid screenshot image task timestamps or assignment' USING ERRCODE = '22023';
            END IF;
            UPDATE public.tasks AS task SET
                status=v_entry ->> 'status',
                sequence_order=(v_entry ->> 'sequence_order')::integer,
                assignee_id=NULLIF(v_entry ->> 'assignee_id','null')::uuid,
                reviewer_id=NULLIF(v_entry ->> 'reviewer_id','null')::uuid,
                assigned_at=NULLIF(v_entry ->> 'assigned_at','null')::timestamptz,
                submitted_at=NULLIF(v_entry ->> 'submitted_at','null')::timestamptz,
                reviewer_claimed_at=NULLIF(v_entry ->> 'reviewer_claimed_at','null')::timestamptz,
                reviewed_at=NULLIF(v_entry ->> 'reviewed_at','null')::timestamptz,
                reject_reason=NULL,reject_reason_type=NULL,skip_reason=NULL,skipped_at=NULL
            WHERE task.id=(v_entry ->> 'task_id')::uuid AND task.project_id=v_project_id
              AND task.display_id=('T-SS-COCO8-' || pg_catalog.lpad(((v_entry ->> 'sequence_order')::integer+1)::text,3,'0'))
              AND task.sequence_order=(v_entry ->> 'sequence_order')::integer;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                RAISE EXCEPTION 'Screenshot image task scope changed' USING ERRCODE = '23514';
            END IF;
            v_count := v_count + v_rows;
        END LOOP;
        v_changed := v_count > 0;
        v_data := pg_catalog.jsonb_build_object('tasks',v_count);

    ELSIF v_operation = 'reconcile_seed_predictions' THEN
        IF (p_request - ARRAY['schema','operation','project_id','task_ids','predicted_task_id','keep_prediction_id','remove_prediction_ids','model_version','fixed_time']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','project_id','task_ids','predicted_task_id','keep_prediction_id','remove_prediction_ids','model_version','fixed_time'])
           OR pg_catalog.jsonb_typeof(p_request -> 'task_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request -> 'remove_prediction_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request -> 'model_version') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'fixed_time') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid screenshot prediction reconciliation request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_task_id := (p_request ->> 'predicted_task_id')::uuid;
        v_prediction_id := (p_request ->> 'keep_prediction_id')::uuid;
        v_fixed_time := (p_request ->> 'fixed_time')::timestamptz;
        v_project_json := aap_workspace_v1.read_project_row(v_project_id)::jsonb;
        SELECT pg_catalog.array_agg(value::uuid ORDER BY value::uuid)
        INTO v_task_ids FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids') AS ids(value);
        IF v_project_json IS NULL OR v_project_json ->> 'display_id' IS DISTINCT FROM 'P-COCO8'
           OR p_request ->> 'model_version' IS DISTINCT FROM 'screenshot-seed:screenshots-2026-08-g'
           OR v_fixed_time IS DISTINCT FROM TIMESTAMPTZ '2026-07-01 09:00:00+00'
           OR v_task_ids IS NULL OR pg_catalog.cardinality(v_task_ids) <> 8
           OR pg_catalog.cardinality(v_task_ids) <> (SELECT pg_catalog.count(DISTINCT value) FROM pg_catalog.unnest(v_task_ids) AS value)
           OR pg_catalog.jsonb_array_length(p_request -> 'remove_prediction_ids') <>
              (SELECT pg_catalog.count(DISTINCT value) FROM pg_catalog.jsonb_array_elements_text(p_request -> 'remove_prediction_ids') AS ids(value))
           OR NOT v_task_id = ANY(v_task_ids)
           OR (SELECT pg_catalog.count(*) FROM public.tasks AS task
               WHERE task.id=ANY(v_task_ids) AND task.project_id=v_project_id
                 AND task.display_id IN ('T-SS-COCO8-001','T-SS-COCO8-002','T-SS-COCO8-003',
                     'T-SS-COCO8-004','T-SS-COCO8-005','T-SS-COCO8-006','T-SS-COCO8-007','T-SS-COCO8-008')
                 AND task.sequence_order=pg_catalog.substring(task.display_id, '([0-9]{3})$')::integer-1) <> 8
           OR NOT EXISTS (SELECT FROM public.predictions AS prediction
                          WHERE prediction.id=v_prediction_id AND prediction.project_id=v_project_id
                            AND prediction.task_id=v_task_id
                            AND prediction.model_version=p_request ->> 'model_version')
           OR EXISTS (SELECT FROM public.predictions AS prediction
                      WHERE prediction.task_id=ANY(v_task_ids)
                        AND prediction.model_version IS DISTINCT FROM p_request ->> 'model_version')
           OR EXISTS (SELECT FROM pg_catalog.jsonb_array_elements_text(p_request -> 'remove_prediction_ids') AS ids(value)
                      WHERE ids.value::uuid=v_prediction_id
                         OR NOT EXISTS (SELECT FROM public.predictions AS prediction
                                        WHERE prediction.id=ids.value::uuid AND prediction.project_id=v_project_id
                                          AND prediction.model_version=p_request ->> 'model_version'
                                          AND prediction.task_id=ANY(v_task_ids)))
           OR (SELECT pg_catalog.count(*) FROM public.predictions AS prediction
               WHERE prediction.project_id=v_project_id AND prediction.model_version=p_request ->> 'model_version')
              <> 1 + pg_catalog.jsonb_array_length(p_request -> 'remove_prediction_ids') THEN
            RAISE EXCEPTION 'Screenshot prediction scope changed' USING ERRCODE = '23514';
        END IF;
        IF pg_catalog.jsonb_array_length(p_request -> 'remove_prediction_ids') > 0 THEN
            UPDATE public.annotations AS annotation
            SET parent_prediction_id=NULL
            WHERE annotation.parent_prediction_id IN (
                SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'remove_prediction_ids') AS ids(value)
            );
            DELETE FROM public.prediction_metas AS meta
            WHERE meta.prediction_id IN (
                SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'remove_prediction_ids') AS ids(value)
            );
            DELETE FROM public.predictions AS prediction
            WHERE prediction.id IN (
                SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'remove_prediction_ids') AS ids(value)
            ) AND prediction.project_id=v_project_id;
            GET DIAGNOSTICS v_predictions = ROW_COUNT;
        END IF;
        SELECT prediction.created_at INTO v_prediction_created_at
        FROM public.predictions AS prediction
        WHERE prediction.id=v_prediction_id AND prediction.task_id=v_task_id
          AND prediction.project_id=v_project_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Screenshot prediction disappeared' USING ERRCODE = '23514';
        END IF;
        IF v_prediction_created_at IS DISTINCT FROM v_fixed_time THEN
            UPDATE public.prediction_metas AS meta SET prediction_created_at=NULL
            WHERE meta.prediction_id=v_prediction_id;
            UPDATE public.predictions AS prediction SET created_at=v_fixed_time
            WHERE prediction.id=v_prediction_id AND prediction.created_at=v_prediction_created_at;
            UPDATE public.prediction_metas AS meta SET
                prediction_created_at=v_fixed_time, created_at=v_fixed_time
            WHERE meta.prediction_id=v_prediction_id;
        END IF;
        UPDATE public.tasks AS task SET total_predictions=CASE WHEN task.id=v_task_id THEN 1 ELSE 0 END
        WHERE task.id=ANY(v_task_ids) AND task.project_id=v_project_id
          AND task.total_predictions IS DISTINCT FROM CASE WHEN task.id=v_task_id THEN 1 ELSE 0 END;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_changed := true;
        v_data := pg_catalog.jsonb_build_object('predictions',v_predictions,'tasks',v_rows);

    ELSIF v_operation = 'reconcile_seed_annotations' THEN
        IF (p_request - ARRAY['schema','operation','project_id','annotations','tasks','annotator_id','fixed_time']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','project_id','annotations','tasks','annotator_id','fixed_time'])
           OR pg_catalog.jsonb_typeof(p_request -> 'annotations') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'annotations') <> 3
           OR pg_catalog.jsonb_typeof(p_request -> 'tasks') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'tasks') <> 8
           OR pg_catalog.jsonb_typeof(p_request -> 'fixed_time') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid screenshot annotation reconciliation request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_fixed_time := (p_request ->> 'fixed_time')::timestamptz;
        v_project_json := aap_workspace_v1.read_project_row(v_project_id)::jsonb;
        IF v_project_json IS NULL OR v_project_json ->> 'display_id' IS DISTINCT FROM 'P-COCO8' THEN
            RAISE EXCEPTION 'Screenshot annotation project scope changed' USING ERRCODE = '23514';
        END IF;
        IF v_fixed_time IS DISTINCT FROM TIMESTAMPTZ '2026-07-01 09:00:00+00' THEN
            RAISE EXCEPTION 'Invalid screenshot annotation timestamp' USING ERRCODE = '22023';
        END IF;
        SELECT pg_catalog.array_agg((entry ->> 'task_id')::uuid ORDER BY (entry ->> 'task_id')::uuid)
        INTO v_task_ids FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS entry;
        IF v_task_ids IS NULL
           OR pg_catalog.cardinality(v_task_ids) <> (SELECT pg_catalog.count(DISTINCT value) FROM pg_catalog.unnest(v_task_ids) AS value)
           OR EXISTS (SELECT FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS entry
                      WHERE (entry - ARRAY['task_id','status','review_round_id','annotation_contributor_ids','review_contributor_ids','review_submitter_id']) IS DISTINCT FROM '{}'::jsonb
                         OR NOT (entry ?& ARRAY['task_id','status','review_round_id','annotation_contributor_ids','review_contributor_ids','review_submitter_id']))
           OR (SELECT pg_catalog.count(*) FROM public.tasks AS task
               WHERE task.id=ANY(v_task_ids) AND task.project_id=v_project_id
                 AND task.display_id IN ('T-SS-COCO8-001','T-SS-COCO8-002','T-SS-COCO8-003',
                     'T-SS-COCO8-004','T-SS-COCO8-005','T-SS-COCO8-006','T-SS-COCO8-007','T-SS-COCO8-008')
                 AND task.sequence_order=pg_catalog.substring(task.display_id, '([0-9]{3})$')::integer-1) <> 8 THEN
            RAISE EXCEPTION 'Invalid screenshot annotation task scope' USING ERRCODE = '22023';
        END IF;
        IF EXISTS (SELECT FROM pg_catalog.jsonb_array_elements(p_request -> 'annotations') AS entry
                   WHERE (entry ->> 'id') NOT IN (
                       '7ab27502-a926-51f1-9f95-a02d6ff657d8',
                       'e54a4e99-6718-59a6-a18f-80cad0e173d4',
                       '86a41fa0-c183-5d9d-acd8-9d1ac6d3134b'
                   ))
           OR (SELECT pg_catalog.count(DISTINCT entry ->> 'id')
               FROM pg_catalog.jsonb_array_elements(p_request -> 'annotations') AS entry) <> 3 THEN
            RAISE EXCEPTION 'Invalid screenshot annotation identity set' USING ERRCODE = '22023';
        END IF;
        IF EXISTS (SELECT FROM public.annotations AS annotation
                   WHERE annotation.task_id=ANY(v_task_ids)
                     AND annotation.id NOT IN (SELECT (entry ->> 'id')::uuid
                                               FROM pg_catalog.jsonb_array_elements(p_request -> 'annotations') AS entry)) THEN
            RAISE EXCEPTION 'Screenshot project contains unmanaged annotations' USING ERRCODE = '23514';
        END IF;
        FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'annotations') AS rows(value)
        LOOP
            IF (v_entry - ARRAY['id','task_id','user_id','geometry','ground_truth','created_at']) IS DISTINCT FROM '{}'::jsonb
               OR NOT (v_entry ?& ARRAY['id','task_id','user_id','geometry','ground_truth','created_at'])
               OR (v_entry ->> 'user_id') IS DISTINCT FROM (p_request ->> 'annotator_id') THEN
                RAISE EXCEPTION 'Invalid screenshot annotation fields' USING ERRCODE = '22023';
            END IF;
            v_id := (v_entry ->> 'id')::uuid;
            v_expected_task_display_id := CASE v_id
                WHEN '7ab27502-a926-51f1-9f95-a02d6ff657d8'::uuid THEN 'T-SS-COCO8-004'
                WHEN 'e54a4e99-6718-59a6-a18f-80cad0e173d4'::uuid THEN 'T-SS-COCO8-005'
                WHEN '86a41fa0-c183-5d9d-acd8-9d1ac6d3134b'::uuid THEN 'T-SS-COCO8-006'
            END;
            v_expected_annotation_time := v_fixed_time + CASE v_id
                WHEN '7ab27502-a926-51f1-9f95-a02d6ff657d8'::uuid THEN INTERVAL '3 hours'
                WHEN 'e54a4e99-6718-59a6-a18f-80cad0e173d4'::uuid THEN INTERVAL '4 hours'
                WHEN '86a41fa0-c183-5d9d-acd8-9d1ac6d3134b'::uuid THEN INTERVAL '5 hours'
            END;
            v_expected_annotation_geometry := pg_catalog.jsonb_build_object(
                'type','bbox','x',CASE v_id
                    WHEN '7ab27502-a926-51f1-9f95-a02d6ff657d8'::uuid THEN 0.18
                    WHEN 'e54a4e99-6718-59a6-a18f-80cad0e173d4'::uuid THEN 0.23
                    WHEN '86a41fa0-c183-5d9d-acd8-9d1ac6d3134b'::uuid THEN 0.28
                END,
                'y',0.45,'w',0.24,'h',0.2
            );
            v_expected_annotation_ground_truth := v_id='86a41fa0-c183-5d9d-acd8-9d1ac6d3134b'::uuid;
            IF NOT EXISTS (SELECT FROM public.tasks AS task
                           WHERE task.id=(v_entry ->> 'task_id')::uuid
                             AND task.project_id=v_project_id
                             AND task.display_id=v_expected_task_display_id)
               OR EXISTS (SELECT FROM public.annotations AS annotation
                          WHERE annotation.id=v_id
                            AND (annotation.task_id IS DISTINCT FROM (v_entry ->> 'task_id')::uuid
                                 OR annotation.project_id IS DISTINCT FROM v_project_id))
               OR pg_catalog.jsonb_typeof(v_entry -> 'geometry') IS DISTINCT FROM 'object'
               OR (v_entry -> 'geometry') IS DISTINCT FROM v_expected_annotation_geometry
               OR (v_entry ->> 'ground_truth')::boolean IS DISTINCT FROM v_expected_annotation_ground_truth
               OR (v_entry ->> 'created_at')::timestamptz IS DISTINCT FROM v_expected_annotation_time THEN
                RAISE EXCEPTION 'Screenshot annotation target changed' USING ERRCODE = '23514';
            END IF;
            INSERT INTO public.annotations (
                id,task_id,project_id,user_id,source,annotation_type,tool_unit_id,
                class_name,geometry,attributes,is_active,was_cancelled,ground_truth,
                version,created_at,updated_at
            ) VALUES (
                v_id,(v_entry ->> 'task_id')::uuid,v_project_id,(v_entry ->> 'user_id')::uuid,
                'manual','bbox','bbox','car',v_entry -> 'geometry',
                '{"seed_managed_by":"screenshot-seed"}'::jsonb,true,false,
                (v_entry ->> 'ground_truth')::boolean,1,
                (v_entry ->> 'created_at')::timestamptz,(v_entry ->> 'created_at')::timestamptz
            )
            ON CONFLICT (id) DO UPDATE SET
                task_id=EXCLUDED.task_id,project_id=EXCLUDED.project_id,user_id=EXCLUDED.user_id,
                source=EXCLUDED.source,annotation_type=EXCLUDED.annotation_type,
                tool_unit_id=EXCLUDED.tool_unit_id,class_name=EXCLUDED.class_name,
                geometry=EXCLUDED.geometry,attributes=EXCLUDED.attributes,
                is_active=EXCLUDED.is_active,was_cancelled=EXCLUDED.was_cancelled,
                ground_truth=EXCLUDED.ground_truth,created_at=EXCLUDED.created_at,
                updated_at=EXCLUDED.updated_at;
            v_annotations := v_annotations + 1;
        END LOOP;
        FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS rows(value)
        LOOP
            v_id := (v_entry ->> 'task_id')::uuid;
            UPDATE public.tasks AS task SET
                total_annotations=CASE WHEN v_id IN (
                    SELECT (annotation ->> 'task_id')::uuid
                    FROM pg_catalog.jsonb_array_elements(p_request -> 'annotations') AS annotation
                ) THEN 1 ELSE 0 END,
                is_labeled=v_id IN (
                    SELECT (annotation ->> 'task_id')::uuid
                    FROM pg_catalog.jsonb_array_elements(p_request -> 'annotations') AS annotation
                ),
                review_round_id=NULLIF(v_entry ->> 'review_round_id','null')::uuid,
                annotation_contributor_ids=v_entry -> 'annotation_contributor_ids',
                review_contributor_ids=CASE
                    WHEN v_entry -> 'review_contributor_ids'='null'::jsonb
                     AND (task.review_contributor_ids IS NULL
                          OR task.review_contributor_ids='null'::jsonb)
                    THEN task.review_contributor_ids
                    ELSE v_entry -> 'review_contributor_ids'
                END,
                review_submitter_id=NULLIF(v_entry ->> 'review_submitter_id','null')::uuid
            WHERE task.id=v_id AND task.project_id=v_project_id
              AND task.status=v_entry ->> 'status';
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                RAISE EXCEPTION 'Screenshot annotation task changed' USING ERRCODE = '23514';
            END IF;
            v_count := v_count + v_rows;
        END LOOP;
        v_changed := true;
        v_data := pg_catalog.jsonb_build_object('annotations',v_annotations,'tasks',v_count);

    ELSIF v_operation = 'sync_seed_batch_counters' THEN
        IF (p_request - ARRAY['schema','operation','project_id','task_ids','batches']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','project_id','task_ids','batches'])
           OR pg_catalog.jsonb_typeof(p_request -> 'task_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'task_ids') <> 8
           OR pg_catalog.jsonb_typeof(p_request -> 'batches') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'batches') <> 5 THEN
            RAISE EXCEPTION 'Invalid screenshot batch counter request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_project_json := aap_workspace_v1.read_project_row(v_project_id)::jsonb;
        IF v_project_json IS NULL OR v_project_json ->> 'display_id' IS DISTINCT FROM 'P-COCO8' THEN
            RAISE EXCEPTION 'Screenshot batch counter project changed' USING ERRCODE = '23514';
        END IF;
        SELECT pg_catalog.array_agg(value::uuid ORDER BY value::uuid)
        INTO v_task_ids FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids') AS ids(value);
        IF pg_catalog.cardinality(v_task_ids) <> 8
           OR pg_catalog.cardinality(v_task_ids) <>
              (SELECT pg_catalog.count(DISTINCT value) FROM pg_catalog.unnest(v_task_ids) AS value)
           OR (SELECT pg_catalog.count(*) FROM public.tasks AS task
               WHERE task.id=ANY(v_task_ids) AND task.project_id=v_project_id
                 AND task.display_id IN ('T-SS-COCO8-001','T-SS-COCO8-002','T-SS-COCO8-003',
                     'T-SS-COCO8-004','T-SS-COCO8-005','T-SS-COCO8-006','T-SS-COCO8-007','T-SS-COCO8-008')
                 AND task.sequence_order=pg_catalog.substring(task.display_id, '([0-9]{3})$')::integer-1) <> 8 THEN
            RAISE EXCEPTION 'Invalid screenshot batch counter task scope' USING ERRCODE = '22023';
        END IF;
        FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'batches') AS rows(value)
        LOOP
            IF (v_entry - ARRAY['id','batch_key']) IS DISTINCT FROM '{}'::jsonb
               OR NOT (v_entry ?& ARRAY['id','batch_key']) THEN
                RAISE EXCEPTION 'Invalid screenshot batch counter scope' USING ERRCODE = '22023';
            END IF;
            v_batch_id := (v_entry ->> 'id')::uuid;
            IF (v_entry ->> 'batch_key') NOT IN ('draft','active','annotating','review','completed') THEN
                RAISE EXCEPTION 'Invalid screenshot batch counter key' USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (SELECT FROM public.task_batches AS batch WHERE batch.id=v_batch_id
                           AND batch.project_id=v_project_id
                           AND batch.display_id=CASE v_entry ->> 'batch_key'
                               WHEN 'draft' THEN 'B-SS-DRAFT' WHEN 'active' THEN 'B-SS-ACTIVE'
                               WHEN 'annotating' THEN 'B-SS-ANNOTATING' WHEN 'review' THEN 'B-SS-REVIEW'
                               WHEN 'completed' THEN 'B-SS-COMPLETE' END) THEN
                RAISE EXCEPTION 'Screenshot batch counter scope changed' USING ERRCODE = '23514';
            END IF;
            UPDATE public.task_batches AS batch SET
                total_tasks=counts.total_tasks,
                completed_tasks=counts.completed_tasks,
                review_tasks=counts.review_tasks,
                approved_tasks=CASE WHEN v_entry ->> 'batch_key'='completed' THEN counts.completed_tasks ELSE 0 END,
                rejected_tasks=0,
                updated_at=pg_catalog.transaction_timestamp()
            FROM (
                SELECT pg_catalog.count(*)::integer AS total_tasks,
                       pg_catalog.count(*) FILTER (WHERE task.status='completed')::integer AS completed_tasks,
                       pg_catalog.count(*) FILTER (WHERE task.status='review')::integer AS review_tasks
                FROM public.tasks AS task WHERE task.batch_id=v_batch_id
                  AND task.id=ANY(v_task_ids) AND task.project_id=v_project_id
            ) AS counts
            WHERE batch.id=v_batch_id AND batch.project_id=v_project_id
              AND (batch.total_tasks,batch.completed_tasks,batch.review_tasks,batch.approved_tasks,batch.rejected_tasks)
                  IS DISTINCT FROM (
                      counts.total_tasks,counts.completed_tasks,counts.review_tasks,
                      CASE WHEN v_entry ->> 'batch_key'='completed' THEN counts.completed_tasks ELSE 0 END,0
                  );
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            v_count := v_count + v_rows;
        END LOOP;
        v_changed := v_count > 0;
        v_data := pg_catalog.jsonb_build_object('batches',v_count);

    ELSIF v_operation = 'pin_seed_task_timestamps' THEN
        IF (p_request - ARRAY['schema','operation','tasks']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','tasks'])
           OR pg_catalog.jsonb_typeof(p_request -> 'tasks') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid screenshot task timestamp request' USING ERRCODE = '22023';
        END IF;
        FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'tasks') AS rows(value)
        LOOP
            IF (v_entry - ARRAY['project_id','project_display_id','task_id','created_at','updated_at']) IS DISTINCT FROM '{}'::jsonb
               OR NOT (v_entry ?& ARRAY['project_id','project_display_id','task_id','created_at','updated_at'])
               OR v_entry ->> 'project_display_id' NOT IN ('P-COCO8','P-VIDEO-DEV','P-PC-DEV','P-PC-MULTI','P-OCR') THEN
                RAISE EXCEPTION 'Invalid screenshot task timestamp fields' USING ERRCODE = '22023';
            END IF;
            v_project_json := aap_workspace_v1.read_project_row((v_entry ->> 'project_id')::uuid)::jsonb;
            IF v_project_json IS NULL OR v_project_json ->> 'display_id' IS DISTINCT FROM v_entry ->> 'project_display_id' THEN
                RAISE EXCEPTION 'Screenshot task project changed' USING ERRCODE = '23514';
            END IF;
            SELECT task.display_id,task.reviewed_at,task.submitted_at,task.assigned_at
            INTO v_task_display_id,v_task_reviewed_at,v_task_submitted_at,v_task_assigned_at
            FROM public.tasks AS task
            WHERE task.id=(v_entry ->> 'task_id')::uuid
              AND task.project_id=(v_entry ->> 'project_id')::uuid
              AND task.display_id LIKE ('T-SS-' || pg_catalog.substring(v_entry ->> 'project_display_id', 3) || '-%');
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Screenshot task timestamp scope changed' USING ERRCODE = '23514';
            END IF;
            v_task_sequence := pg_catalog.substring(v_task_display_id, '([0-9]{3})$')::integer-1;
            v_expected_task_created_at := TIMESTAMPTZ '2026-07-01 09:00:00+00'
                + v_task_sequence * INTERVAL '1 minute';
            v_expected_task_updated_at := COALESCE(
                v_task_reviewed_at,v_task_submitted_at,v_task_assigned_at,
                v_expected_task_created_at
            );
            IF (v_entry ->> 'created_at')::timestamptz IS DISTINCT FROM v_expected_task_created_at
               OR (v_entry ->> 'updated_at')::timestamptz IS DISTINCT FROM v_expected_task_updated_at THEN
                RAISE EXCEPTION 'Invalid screenshot task timestamps' USING ERRCODE = '22023';
            END IF;
            UPDATE public.tasks AS task SET
                created_at=(v_entry ->> 'created_at')::timestamptz,
                updated_at=(v_entry ->> 'updated_at')::timestamptz
            WHERE task.id=(v_entry ->> 'task_id')::uuid
              AND task.project_id=(v_entry ->> 'project_id')::uuid
              AND task.display_id LIKE ('T-SS-' || pg_catalog.substring(v_entry ->> 'project_display_id', 3) || '-%');
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                RAISE EXCEPTION 'Screenshot task timestamp scope changed' USING ERRCODE = '23514';
            END IF;
            v_count := v_count + v_rows;
        END LOOP;
        v_changed := v_count > 0;
        v_data := pg_catalog.jsonb_build_object('tasks',v_count);

    ELSIF v_operation = 'cleanup_ocr_rows' THEN
        IF (p_request - ARRAY['schema','operation','project_key','project_id','task_id','tracker_job_ids','annotation_ids']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','project_key','project_id','task_id','tracker_job_ids','annotation_ids'])
           OR pg_catalog.jsonb_typeof(p_request -> 'tracker_job_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request -> 'annotation_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid screenshot cleanup request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_key := p_request ->> 'project_key';
        v_project_display_id := CASE v_project_key WHEN 'image_demo' THEN 'P-COCO8' WHEN 'video_demo' THEN 'P-VIDEO-DEV' WHEN 'ocr_demo' THEN 'P-OCR' END;
        v_project_json := aap_workspace_v1.read_project_row(v_project_id)::jsonb;
        IF v_project_display_id IS NULL OR v_project_json IS NULL
           OR v_project_json ->> 'display_id' IS DISTINCT FROM v_project_display_id
           OR NOT EXISTS (SELECT FROM public.tasks AS task WHERE task.id=v_task_id AND task.project_id=v_project_id
                          AND task.display_id LIKE ('T-SS-' || pg_catalog.substring(v_project_display_id, 3) || '-%')) THEN
            RAISE EXCEPTION 'Screenshot cleanup task scope changed' USING ERRCODE = '23514';
        END IF;
        DELETE FROM public.video_tracker_jobs AS job
        WHERE job.id IN (SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'tracker_job_ids') AS ids(value))
          AND job.task_id=v_task_id;
        GET DIAGNOSTICS v_tracker_jobs = ROW_COUNT;
        DELETE FROM public.annotations AS annotation
        WHERE annotation.id IN (SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'annotation_ids') AS ids(value))
          AND annotation.task_id=v_task_id;
        GET DIAGNOSTICS v_annotations = ROW_COUNT;
        v_changed := v_tracker_jobs + v_annotations > 0;
        v_data := pg_catalog.jsonb_build_object('video_tracker_jobs',v_tracker_jobs,'annotations',v_annotations);

    ELSIF v_operation = 'delete_ocr_predictions' THEN
        IF (p_request - ARRAY['schema','operation','project_key','project_id','task_id','prediction_ids']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','project_key','project_id','task_id','prediction_ids'])
           OR pg_catalog.jsonb_typeof(p_request -> 'prediction_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid screenshot prediction cleanup request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_key := p_request ->> 'project_key';
        v_project_display_id := CASE v_project_key WHEN 'image_demo' THEN 'P-COCO8' WHEN 'video_demo' THEN 'P-VIDEO-DEV' WHEN 'ocr_demo' THEN 'P-OCR' END;
        v_project_json := aap_workspace_v1.read_project_row(v_project_id)::jsonb;
        IF v_project_display_id IS NULL OR v_project_json IS NULL
           OR v_project_json ->> 'display_id' IS DISTINCT FROM v_project_display_id
           OR NOT EXISTS (SELECT FROM public.tasks AS task WHERE task.id=v_task_id AND task.project_id=v_project_id
                          AND task.display_id LIKE ('T-SS-' || pg_catalog.substring(v_project_display_id, 3) || '-%')) THEN
            RAISE EXCEPTION 'Screenshot prediction cleanup scope changed' USING ERRCODE = '23514';
        END IF;
        SELECT pg_catalog.count(*) INTO v_rows FROM public.predictions AS prediction
        WHERE prediction.id IN (SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'prediction_ids') AS ids(value));
        IF v_rows > 0 AND v_rows <> pg_catalog.jsonb_array_length(p_request -> 'prediction_ids') THEN
            RAISE EXCEPTION 'Screenshot prediction set is incomplete' USING ERRCODE = '23514';
        END IF;
        IF EXISTS (SELECT FROM public.predictions AS prediction
                   WHERE prediction.id IN (SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'prediction_ids') AS ids(value))
                     AND (prediction.task_id<>v_task_id OR prediction.project_id<>v_project_id)) THEN
            RAISE EXCEPTION 'Screenshot prediction is outside the declared task' USING ERRCODE = '23514';
        END IF;
        UPDATE public.annotations AS annotation SET parent_prediction_id=NULL
        WHERE annotation.parent_prediction_id IN (
            SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'prediction_ids') AS ids(value)
        );
        DELETE FROM public.prediction_metas AS meta
        WHERE meta.prediction_id IN (
            SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'prediction_ids') AS ids(value)
        );
        DELETE FROM public.predictions AS prediction
        WHERE prediction.id IN (SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'prediction_ids') AS ids(value))
          AND prediction.task_id=v_task_id;
        GET DIAGNOSTICS v_predictions = ROW_COUNT;
        SELECT pg_catalog.count(*) INTO v_total FROM public.predictions AS prediction WHERE prediction.task_id=v_task_id;
        UPDATE public.tasks AS task SET total_predictions=v_total,
            updated_at=pg_catalog.transaction_timestamp()
        WHERE task.id=v_task_id AND task.project_id=v_project_id
          AND task.total_predictions IS DISTINCT FROM v_total;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_changed := v_predictions > 0 OR v_rows > 0;
        v_data := pg_catalog.jsonb_build_object('predictions',v_predictions,'tasks',v_rows,'remaining_predictions',v_total);

    ELSIF v_operation = 'finish_ocr_cleanup' THEN
        IF (p_request - ARRAY['schema','operation','project_key','project_id','task_id','expected_status']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','project_key','project_id','task_id','expected_status'])
           OR p_request ->> 'expected_status' NOT IN ('pending','in_progress','review','completed') THEN
            RAISE EXCEPTION 'Invalid screenshot cleanup completion request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_key := p_request ->> 'project_key';
        v_project_display_id := CASE v_project_key WHEN 'image_demo' THEN 'P-COCO8' WHEN 'video_demo' THEN 'P-VIDEO-DEV' WHEN 'ocr_demo' THEN 'P-OCR' END;
        v_project_json := aap_workspace_v1.read_project_row(v_project_id)::jsonb;
        IF v_project_display_id IS NULL OR v_project_json IS NULL
           OR v_project_json ->> 'display_id' IS DISTINCT FROM v_project_display_id
           OR NOT EXISTS (SELECT FROM public.tasks AS task WHERE task.id=v_task_id AND task.project_id=v_project_id
                          AND task.display_id LIKE ('T-SS-' || pg_catalog.substring(v_project_display_id, 3) || '-%')) THEN
            RAISE EXCEPTION 'Screenshot cleanup completion scope changed' USING ERRCODE = '23514';
        END IF;
        SELECT pg_catalog.count(*) INTO v_annotations FROM public.annotations AS annotation
        WHERE annotation.task_id=v_task_id AND annotation.is_active IS TRUE AND annotation.was_cancelled IS FALSE;
        UPDATE public.tasks AS task SET total_annotations=v_annotations,
            is_labeled=(v_annotations > 0),status=p_request ->> 'expected_status',
            updated_at=pg_catalog.transaction_timestamp()
        WHERE task.id=v_task_id AND task.project_id=v_project_id
          AND (task.total_annotations,task.is_labeled,task.status)
              IS DISTINCT FROM (v_annotations::integer,(v_annotations > 0),p_request ->> 'expected_status');
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_changed := v_rows > 0;
        v_data := pg_catalog.jsonb_build_object('remaining_annotations',v_annotations,'tasks',v_rows);

    ELSIF v_operation = 'cleanup_recovery_rows' THEN
        IF (p_request - ARRAY['schema','operation','project_id','task_id','failed_prediction_ids','prediction_ids']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','project_id','task_id','failed_prediction_ids','prediction_ids'])
           OR pg_catalog.jsonb_typeof(p_request -> 'failed_prediction_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request -> 'prediction_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid screenshot recovery cleanup request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_json := aap_workspace_v1.read_project_row(v_project_id)::jsonb;
        IF v_project_json IS NULL OR v_project_json ->> 'display_id' IS DISTINCT FROM 'P-OCR'
           OR NOT EXISTS (SELECT FROM public.tasks AS task WHERE task.id=v_task_id AND task.project_id=v_project_id
                          AND task.display_id LIKE 'T-SS-OCR-%') THEN
            RAISE EXCEPTION 'Screenshot recovery task scope changed' USING ERRCODE = '23514';
        END IF;
        IF EXISTS (SELECT FROM public.predictions AS prediction
                   WHERE prediction.id IN (SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'prediction_ids') AS ids(value))
                     AND (prediction.project_id<>v_project_id OR prediction.task_id<>v_task_id)) THEN
            RAISE EXCEPTION 'Retry prediction escaped screenshot task' USING ERRCODE = '23514';
        END IF;
        DELETE FROM public.prediction_metas AS meta
        WHERE meta.prediction_id IN (SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'prediction_ids') AS ids(value));
        DELETE FROM public.predictions AS prediction
        WHERE prediction.id IN (SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'prediction_ids') AS ids(value))
          AND prediction.project_id=v_project_id AND prediction.task_id=v_task_id;
        GET DIAGNOSTICS v_predictions = ROW_COUNT;
        DELETE FROM public.failed_predictions AS failed
        WHERE failed.id IN (SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'failed_prediction_ids') AS ids(value))
          AND failed.project_id=v_project_id AND failed.task_id=v_task_id;
        GET DIAGNOSTICS v_failed_predictions = ROW_COUNT;
        SELECT pg_catalog.count(*) INTO v_total FROM public.predictions AS prediction WHERE prediction.task_id=v_task_id;
        UPDATE public.tasks AS task SET total_predictions=v_total,
            updated_at=pg_catalog.transaction_timestamp()
        WHERE task.id=v_task_id AND task.project_id=v_project_id
          AND task.total_predictions IS DISTINCT FROM v_total;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_changed := v_predictions + v_failed_predictions + v_rows > 0;
        v_data := pg_catalog.jsonb_build_object('predictions',v_predictions,'failed_predictions',v_failed_predictions,
            'tasks',v_rows,'remaining_predictions',v_total);

    ELSIF v_operation = 'restore_review_reject' THEN
        IF (p_request - ARRAY['schema','operation','project_id','task_id','reviewer_id','expected_status','review_round_id','review_contributor_ids','review_submitter_id','reason_texts']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','project_id','task_id','reviewer_id','expected_status','review_round_id','review_contributor_ids','review_submitter_id','reason_texts'])
           OR p_request ->> 'expected_status' IS DISTINCT FROM 'review'
           OR pg_catalog.jsonb_typeof(p_request -> 'reason_texts') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'reason_texts') <> 2
           OR (p_request -> 'reason_texts') IS DISTINCT FROM
              '["车辆边界存在偏移，请重新贴合目标外沿。","标注框偏移，请重新对齐目标边缘（演示）"]'::jsonb THEN
            RAISE EXCEPTION 'Invalid screenshot review restore request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_json := aap_workspace_v1.read_project_row(v_project_id)::jsonb;
        IF v_project_json IS NULL OR v_project_json ->> 'display_id' IS DISTINCT FROM 'P-COCO8'
           OR NOT EXISTS (SELECT FROM public.tasks AS task WHERE task.id=v_task_id AND task.project_id=v_project_id
                          AND task.display_id LIKE 'T-SS-COCO8-%') THEN
            RAISE EXCEPTION 'Screenshot review project scope changed' USING ERRCODE = '23514';
        END IF;
        SELECT task.reviewer_id INTO v_task_reviewer_id FROM public.tasks AS task
        WHERE task.id=v_task_id AND task.project_id=v_project_id;
        IF NOT FOUND OR v_task_reviewer_id IS DISTINCT FROM (p_request ->> 'reviewer_id')::uuid THEN
            RAISE EXCEPTION 'Screenshot review task scope changed' USING ERRCODE = '23514';
        END IF;
        DELETE FROM public.annotation_feedbacks AS feedback
        WHERE feedback.task_id=v_task_id
          AND feedback.kind='reject' AND feedback.author_id=(p_request ->> 'reviewer_id')::uuid
          AND feedback.body IN (SELECT value #>> '{}' FROM pg_catalog.jsonb_array_elements(p_request -> 'reason_texts') AS reasons(value));
        GET DIAGNOSTICS v_feedbacks = ROW_COUNT;
        UPDATE public.tasks AS task SET
            status='review',reviewed_at=NULL,reject_reason_type=NULL,reject_reason=NULL,
            reviewer_claimed_at=NULL,
            review_round_id=COALESCE(task.review_round_id,(p_request ->> 'review_round_id')::uuid),
            review_contributor_ids=CASE
                    WHEN p_request -> 'review_contributor_ids'='null'::jsonb
                     AND (task.review_contributor_ids IS NULL
                          OR task.review_contributor_ids='null'::jsonb)
                    THEN task.review_contributor_ids
                    ELSE p_request -> 'review_contributor_ids'
                END,
            review_submitter_id=NULLIF(p_request ->> 'review_submitter_id','null')::uuid,
            updated_at=pg_catalog.transaction_timestamp()
        WHERE task.id=v_task_id AND task.project_id=v_project_id
          AND (task.status,task.reviewed_at,task.reject_reason_type,task.reject_reason,
               task.reviewer_claimed_at,task.review_round_id,task.review_contributor_ids,task.review_submitter_id)
              IS DISTINCT FROM ('review',NULL,NULL,NULL,NULL,
                  COALESCE(task.review_round_id,(p_request ->> 'review_round_id')::uuid),
                  CASE
                    WHEN p_request -> 'review_contributor_ids'='null'::jsonb
                     AND (task.review_contributor_ids IS NULL
                          OR task.review_contributor_ids='null'::jsonb)
                    THEN task.review_contributor_ids
                    ELSE p_request -> 'review_contributor_ids'
                END,
                  NULLIF(p_request ->> 'review_submitter_id','null')::uuid);
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_changed := v_feedbacks + v_rows > 0;
        v_data := pg_catalog.jsonb_build_object('feedbacks',v_feedbacks,'tasks',v_rows);

    ELSE
        RAISE EXCEPTION 'Unsupported screenshot operations operation' USING ERRCODE = '22023';
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'operation',v_operation,'changed',v_changed,'counts',v_data
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.screenshot_operations_command(jsonb) FROM PUBLIC;
