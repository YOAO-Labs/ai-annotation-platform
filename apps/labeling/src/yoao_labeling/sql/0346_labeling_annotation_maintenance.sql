CREATE FUNCTION aap_labeling_v1.annotation_maintenance_command(p_request jsonb)
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
    v_expected text[] := ARRAY['schema','operation'];
    v_field text;
    v_item jsonb;
    v_row record;
    v_project_id uuid;
    v_annotation_ids uuid[];
    v_task_ids uuid[];
    v_batch_ids uuid[];
    v_annotations jsonb := '[]'::jsonb;
    v_tasks jsonb := '[]'::jsonb;
    v_batches jsonb := '[]'::jsonb;
    v_affected uuid[] := ARRAY[]::uuid[];
    v_deactivated uuid[] := ARRAY[]::uuid[];
    v_count integer := 0;
    v_orphans integer := 0;
    v_attribute_counts jsonb := '{}'::jsonb;
    v_attributes jsonb;
    v_data jsonb;
    v_phase text := 'task_busy';
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-annotation-maintenance/v1' THEN
        RAISE EXCEPTION 'Invalid Annotation maintenance request' USING ERRCODE='22023';
    END IF;
    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'read_draft' THEN v_expected := v_expected || ARRAY['task_id','user_id'];
        WHEN 'save_draft' THEN v_expected := v_expected || ARRAY[
            'task_id','user_id','draft_id','existing','write_result','result'];
        WHEN 'read_cleanup_candidates' THEN v_expected := v_expected || ARRAY['project_id'];
        WHEN 'observe_cleanup_tasks' THEN v_expected := v_expected || ARRAY['task_ids'];
        WHEN 'cleanup_schema' THEN v_expected := v_expected || ARRAY[
            'project_id','class_names','attribute_keys','annotations','tasks'];
        ELSE RAISE EXCEPTION 'Invalid Annotation maintenance operation' USING ERRCODE='22023';
    END CASE;
    SELECT array_agg(key ORDER BY key) INTO v_keys FROM jsonb_object_keys(p_request) AS fields(key);
    SELECT array_agg(key ORDER BY key) INTO v_expected FROM unnest(v_expected) AS fields(key);
    IF v_keys IS DISTINCT FROM v_expected THEN
        RAISE EXCEPTION 'Invalid Annotation maintenance fields' USING ERRCODE='22023';
    END IF;
    FOREACH v_field IN ARRAY ARRAY['project_id','task_id','user_id','draft_id'] LOOP
        IF p_request ? v_field AND (jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
            OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
            RAISE EXCEPTION 'Invalid Annotation maintenance identity' USING ERRCODE='22023';
        END IF;
    END LOOP;
    IF v_operation = 'read_draft' THEN
        FOR v_row IN SELECT draft.id, draft.task_id, draft.annotation_id, draft.user_id, draft.result, draft.was_postponed, draft.created_at, draft.updated_at
            FROM public.annotation_drafts AS draft
            WHERE draft.task_id=(p_request ->> 'task_id')::uuid
              AND draft.user_id=(p_request ->> 'user_id')::uuid
        LOOP
            v_count := v_count+1;
            IF v_count>1 THEN
                RAISE EXCEPTION 'Multiple rows were found when one or none was required' USING ERRCODE='21000';
            END IF;
            v_data := (jsonb_build_object('id',v_row.id, 'task_id',v_row.task_id, 'annotation_id',v_row.annotation_id, 'user_id',v_row.user_id, 'result',v_row.result, 'was_postponed',v_row.was_postponed, 'created_at',v_row.created_at, 'updated_at',v_row.updated_at));
        END LOOP;
        v_data := jsonb_build_object('operation',v_operation,'draft',v_data);
    ELSIF v_operation = 'save_draft' THEN
        IF jsonb_typeof(p_request -> 'existing') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_request -> 'write_result') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_request -> 'result') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid Annotation draft payload' USING ERRCODE='22023';
        END IF;
        IF (p_request ->> 'existing')::boolean THEN
            IF (p_request ->> 'write_result')::boolean THEN
                UPDATE public.annotation_drafts AS draft
                SET result=p_request -> 'result',updated_at=transaction_timestamp()
                WHERE draft.id=(p_request ->> 'draft_id')::uuid
                  AND draft.task_id=(p_request ->> 'task_id')::uuid
                  AND draft.user_id=(p_request ->> 'user_id')::uuid;
                IF NOT FOUND THEN
                    RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_maintenance_error',
                        DETAIL='{"reason":"draft_target_changed"}';
                END IF;
            END IF;
        ELSE
            INSERT INTO public.annotation_drafts(id,task_id,user_id,result,was_postponed)
            VALUES ((p_request ->> 'draft_id')::uuid,(p_request ->> 'task_id')::uuid,
                (p_request ->> 'user_id')::uuid,p_request -> 'result',false);
        END IF;
        SELECT draft.id, draft.task_id, draft.annotation_id, draft.user_id, draft.result, draft.was_postponed, draft.created_at, draft.updated_at INTO v_row FROM public.annotation_drafts AS draft
        WHERE draft.id=(p_request ->> 'draft_id')::uuid
          AND draft.task_id=(p_request ->> 'task_id')::uuid
          AND draft.user_id=(p_request ->> 'user_id')::uuid;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_maintenance_error',
                DETAIL='{"reason":"draft_target_changed"}';
        END IF;
        v_data := jsonb_build_object('operation',v_operation,'draft',(jsonb_build_object('id',v_row.id, 'task_id',v_row.task_id, 'annotation_id',v_row.annotation_id, 'user_id',v_row.user_id, 'result',v_row.result, 'was_postponed',v_row.was_postponed, 'created_at',v_row.created_at, 'updated_at',v_row.updated_at)));
    ELSIF v_operation = 'read_cleanup_candidates' THEN
        FOR v_row IN SELECT annotation.id, annotation.task_id, annotation.project_id, annotation.video_segment_id, annotation.user_id, annotation.source, annotation.annotation_type, annotation.tool_unit_id, annotation.class_name, annotation.geometry, annotation.confidence, annotation.parent_prediction_id, annotation.parent_annotation_id, annotation.track_id, annotation.scene_track_id, annotation.sensor_dataset_item_id, annotation.sensor_role, annotation.sensor_visibility, annotation.calibration_revision, annotation.calibration_digest, annotation.temporal_role, annotation.lead_time, annotation.was_cancelled, annotation.ground_truth, annotation.is_active, annotation.attributes, annotation.attributes_meta, annotation.z_order, annotation.is_locked, annotation.is_hidden, annotation.version, annotation.created_at, annotation.updated_at FROM public.annotations AS annotation
            WHERE annotation.project_id=(p_request ->> 'project_id')::uuid
              AND annotation.is_active IS TRUE
        LOOP
            v_annotations := v_annotations || jsonb_build_array((jsonb_build_object('id',v_row.id, 'task_id',v_row.task_id, 'project_id',v_row.project_id, 'video_segment_id',v_row.video_segment_id, 'user_id',v_row.user_id, 'source',v_row.source, 'annotation_type',v_row.annotation_type, 'tool_unit_id',v_row.tool_unit_id, 'class_name',v_row.class_name, 'geometry',v_row.geometry, 'confidence',v_row.confidence, 'parent_prediction_id',v_row.parent_prediction_id, 'parent_annotation_id',v_row.parent_annotation_id, 'track_id',v_row.track_id, 'scene_track_id',v_row.scene_track_id, 'sensor_dataset_item_id',v_row.sensor_dataset_item_id, 'sensor_role',v_row.sensor_role, 'sensor_visibility',v_row.sensor_visibility, 'calibration_revision',v_row.calibration_revision, 'calibration_digest',v_row.calibration_digest, 'temporal_role',v_row.temporal_role, 'lead_time',v_row.lead_time, 'was_cancelled',v_row.was_cancelled, 'ground_truth',v_row.ground_truth) || jsonb_build_object('is_active',v_row.is_active, 'attributes',v_row.attributes, 'attributes_meta',v_row.attributes_meta, 'z_order',v_row.z_order, 'is_locked',v_row.is_locked, 'is_hidden',v_row.is_hidden, 'version',v_row.version, 'created_at',v_row.created_at, 'updated_at',v_row.updated_at)));
        END LOOP;
        v_data := jsonb_build_object('operation',v_operation,'annotations',v_annotations);
    ELSIF v_operation = 'observe_cleanup_tasks' THEN
        IF jsonb_typeof(p_request -> 'task_ids') IS DISTINCT FROM 'array'
           OR EXISTS (SELECT FROM jsonb_array_elements(p_request -> 'task_ids') AS item(value)
                      WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'string') THEN
            RAISE EXCEPTION 'Invalid Annotation cleanup task identities' USING ERRCODE='22023';
        END IF;
        SELECT COALESCE(array_agg(value::uuid),ARRAY[]::uuid[]) INTO v_task_ids
        FROM jsonb_array_elements_text(p_request -> 'task_ids') AS item(value);
        FOR v_row IN SELECT task.id,task.project_id,task.batch_id FROM public.tasks AS task
            WHERE task.id=ANY(v_task_ids) ORDER BY task.id
        LOOP
            v_tasks := v_tasks || jsonb_build_array(jsonb_build_object(
                'id',v_row.id,'project_id',v_row.project_id,'batch_id',v_row.batch_id));
        END LOOP;
        v_data := jsonb_build_object('operation',v_operation,'tasks',v_tasks);
    ELSE
        v_project_id := (p_request ->> 'project_id')::uuid;
        FOREACH v_field IN ARRAY ARRAY['class_names','attribute_keys','annotations','tasks'] LOOP
            IF jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION 'Invalid Annotation cleanup scope' USING ERRCODE='22023';
            END IF;
        END LOOP;
        FOREACH v_field IN ARRAY ARRAY['class_names','attribute_keys'] LOOP
            IF EXISTS (SELECT FROM jsonb_array_elements(p_request -> v_field) AS item(value)
                       WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'string') THEN
                RAISE EXCEPTION 'Invalid Annotation cleanup schema' USING ERRCODE='22023';
            END IF;
        END LOOP;
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'annotations') LOOP
            SELECT array_agg(key ORDER BY key) INTO v_keys FROM jsonb_object_keys(v_item) AS fields(key);
            IF v_keys IS DISTINCT FROM ARRAY['id','task_id']::text[]
               OR jsonb_typeof(v_item -> 'id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_item -> 'task_id') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid Annotation cleanup reference' USING ERRCODE='22023';
            END IF;
        END LOOP;
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'tasks') LOOP
            SELECT array_agg(key ORDER BY key) INTO v_keys FROM jsonb_object_keys(v_item) AS fields(key);
            IF v_keys IS DISTINCT FROM ARRAY['batch_id','id','project_id']::text[]
               OR jsonb_typeof(v_item -> 'id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_item -> 'project_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_item -> 'batch_id') NOT IN ('string','null') THEN
                RAISE EXCEPTION 'Invalid Annotation cleanup task reference' USING ERRCODE='22023';
            END IF;
        END LOOP;
        SELECT COALESCE(array_agg((value ->> 'id')::uuid),ARRAY[]::uuid[]) INTO v_annotation_ids
        FROM jsonb_array_elements(p_request -> 'annotations') AS item(value);
        SELECT COALESCE(array_agg((value ->> 'id')::uuid),ARRAY[]::uuid[]),
               COALESCE(array_agg(DISTINCT (value ->> 'batch_id')::uuid)
                   FILTER (WHERE value ->> 'batch_id' IS NOT NULL),ARRAY[]::uuid[])
        INTO v_task_ids,v_batch_ids FROM jsonb_array_elements(p_request -> 'tasks') AS item(value);
        IF cardinality(v_annotation_ids)<>(SELECT count(DISTINCT id) FROM unnest(v_annotation_ids) AS item(id))
           OR cardinality(v_task_ids)<>(SELECT count(DISTINCT id) FROM unnest(v_task_ids) AS item(id))
           OR EXISTS (SELECT FROM jsonb_array_elements(p_request -> 'annotations') AS item(value)
                      WHERE NOT ((value ->> 'task_id')::uuid=ANY(v_task_ids)))
           OR EXISTS (SELECT FROM jsonb_array_elements(p_request -> 'tasks') AS item(value)
                      WHERE (value ->> 'project_id')::uuid IS DISTINCT FROM v_project_id
                         OR NOT EXISTS (SELECT FROM jsonb_array_elements(p_request -> 'annotations') AS annotation(value)
                                        WHERE (annotation.value ->> 'task_id')::uuid=(item.value ->> 'id')::uuid)) THEN
            RAISE EXCEPTION 'Invalid Annotation cleanup scope references' USING ERRCODE='22023';
        END IF;
        v_phase := 'batch_busy';
        FOR v_row IN SELECT batch.id, batch.project_id, batch.dataset_id, batch.display_id, batch.name, batch.description, batch.status, batch.priority, batch.deadline, batch.assigned_user_ids, batch.total_tasks, batch.completed_tasks, batch.review_tasks, batch.approved_tasks, batch.rejected_tasks, batch.created_by, batch.created_at, batch.updated_at, batch.review_feedback, batch.reviewed_at, batch.reviewed_by, batch.annotator_id, batch.reviewer_id, batch.admin_locked, batch.admin_lock_reason, batch.admin_locked_at, batch.admin_locked_by FROM public.task_batches AS batch
            WHERE batch.id=ANY(v_batch_ids) ORDER BY batch.id FOR UPDATE NOWAIT
        LOOP
            IF v_row.project_id IS DISTINCT FROM v_project_id THEN
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_maintenance_error',DETAIL='{"reason":"task_target_changed"}';
            END IF;
            v_batches := v_batches || jsonb_build_array((jsonb_build_object('id',v_row.id, 'project_id',v_row.project_id, 'dataset_id',v_row.dataset_id, 'display_id',v_row.display_id, 'name',v_row.name, 'description',v_row.description, 'status',v_row.status, 'priority',v_row.priority, 'deadline',v_row.deadline, 'assigned_user_ids',v_row.assigned_user_ids, 'total_tasks',v_row.total_tasks, 'completed_tasks',v_row.completed_tasks, 'review_tasks',v_row.review_tasks, 'approved_tasks',v_row.approved_tasks, 'rejected_tasks',v_row.rejected_tasks, 'created_by',v_row.created_by, 'created_at',v_row.created_at, 'updated_at',v_row.updated_at, 'review_feedback',v_row.review_feedback, 'reviewed_at',v_row.reviewed_at, 'reviewed_by',v_row.reviewed_by, 'annotator_id',v_row.annotator_id, 'reviewer_id',v_row.reviewer_id, 'admin_locked',v_row.admin_locked) || jsonb_build_object('admin_lock_reason',v_row.admin_lock_reason, 'admin_locked_at',v_row.admin_locked_at, 'admin_locked_by',v_row.admin_locked_by)));
        END LOOP;
        IF jsonb_array_length(v_batches)<>cardinality(v_batch_ids) THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_maintenance_error',DETAIL='{"reason":"task_target_changed"}';
        END IF;
        v_phase := 'task_busy';
        FOR v_row IN SELECT task.id, task.project_id, task.dataset_item_id, task.display_id, task.file_name, task.file_path, task.file_type, task.media_source_generation, task.media_object_identity, task.media_source_drifted, task.tags, task.status, task.assignee_id, task.assignee_is_override, task.assigned_at, task.is_labeled, task.overlap, task.total_annotations, task.total_predictions, task.precomputed_agreement, task.sequence_order, task.thumbnail_path, task.blurhash, task.batch_id, task.version, task.submitted_at, task.reviewer_id, task.reviewer_is_override, task.reviewer_claimed_at, task.review_round_id, task.first_review_eligible, task.first_reviewed_at, task.first_review_result, task.first_review_contributor_ids, task.annotation_contributor_ids, task.review_contributor_ids, task.review_submitter_id, task.reviewed_at, task.reject_reason, task.reject_reason_type, task.skip_reason, task.skipped_at, task.reopened_count, task.last_reopened_at, task.created_at, task.updated_at FROM public.tasks AS task
            WHERE task.id=ANY(v_task_ids) ORDER BY task.id FOR UPDATE NOWAIT
        LOOP
            IF v_row.project_id IS DISTINCT FROM v_project_id OR v_row.batch_id IS DISTINCT FROM (
                SELECT (value ->> 'batch_id')::uuid FROM jsonb_array_elements(p_request -> 'tasks') AS item(value)
                WHERE (value ->> 'id')::uuid=v_row.id
            ) THEN
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_maintenance_error',DETAIL='{"reason":"task_target_changed"}';
            END IF;
            v_tasks := v_tasks || jsonb_build_array((jsonb_build_object('id',v_row.id, 'project_id',v_row.project_id, 'dataset_item_id',v_row.dataset_item_id, 'display_id',v_row.display_id, 'file_name',v_row.file_name, 'file_path',v_row.file_path, 'file_type',v_row.file_type, 'media_source_generation',v_row.media_source_generation, 'media_object_identity',v_row.media_object_identity, 'media_source_drifted',v_row.media_source_drifted, 'tags',v_row.tags, 'status',v_row.status, 'assignee_id',v_row.assignee_id, 'assignee_is_override',v_row.assignee_is_override, 'assigned_at',v_row.assigned_at, 'is_labeled',v_row.is_labeled, 'overlap',v_row.overlap, 'total_annotations',v_row.total_annotations, 'total_predictions',v_row.total_predictions, 'precomputed_agreement',v_row.precomputed_agreement, 'sequence_order',v_row.sequence_order, 'thumbnail_path',v_row.thumbnail_path, 'blurhash',v_row.blurhash, 'batch_id',v_row.batch_id) || jsonb_build_object('version',v_row.version, 'submitted_at',v_row.submitted_at, 'reviewer_id',v_row.reviewer_id, 'reviewer_is_override',v_row.reviewer_is_override, 'reviewer_claimed_at',v_row.reviewer_claimed_at, 'review_round_id',v_row.review_round_id, 'first_review_eligible',v_row.first_review_eligible, 'first_reviewed_at',v_row.first_reviewed_at, 'first_review_result',v_row.first_review_result, 'first_review_contributor_ids',v_row.first_review_contributor_ids, 'annotation_contributor_ids',v_row.annotation_contributor_ids, 'review_contributor_ids',v_row.review_contributor_ids, 'review_submitter_id',v_row.review_submitter_id, 'reviewed_at',v_row.reviewed_at, 'reject_reason',v_row.reject_reason, 'reject_reason_type',v_row.reject_reason_type, 'skip_reason',v_row.skip_reason, 'skipped_at',v_row.skipped_at, 'reopened_count',v_row.reopened_count, 'last_reopened_at',v_row.last_reopened_at, 'created_at',v_row.created_at, 'updated_at',v_row.updated_at)));
        END LOOP;
        IF jsonb_array_length(v_tasks)<>cardinality(v_task_ids) THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_maintenance_error',DETAIL='{"reason":"task_target_changed"}';
        END IF;
        v_phase := 'annotation_busy';
        FOR v_row IN SELECT annotation.id, annotation.task_id, annotation.project_id, annotation.video_segment_id, annotation.user_id, annotation.source, annotation.annotation_type, annotation.tool_unit_id, annotation.class_name, annotation.geometry, annotation.confidence, annotation.parent_prediction_id, annotation.parent_annotation_id, annotation.track_id, annotation.scene_track_id, annotation.sensor_dataset_item_id, annotation.sensor_role, annotation.sensor_visibility, annotation.calibration_revision, annotation.calibration_digest, annotation.temporal_role, annotation.lead_time, annotation.was_cancelled, annotation.ground_truth, annotation.is_active, annotation.attributes, annotation.attributes_meta, annotation.z_order, annotation.is_locked, annotation.is_hidden, annotation.version, annotation.created_at, annotation.updated_at FROM public.annotations AS annotation
            WHERE annotation.id=ANY(v_annotation_ids) ORDER BY annotation.id FOR UPDATE NOWAIT
        LOOP
            IF v_row.project_id IS DISTINCT FROM v_project_id
               OR NOT EXISTS (SELECT FROM jsonb_array_elements(p_request -> 'annotations') AS item(value)
                              WHERE (value ->> 'id')::uuid=v_row.id AND (value ->> 'task_id')::uuid=v_row.task_id)
               OR NOT (v_row.task_id=ANY(v_task_ids)) THEN
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_maintenance_error',DETAIL='{"reason":"annotation_target_changed"}';
            END IF;
            v_annotations := v_annotations || jsonb_build_array((jsonb_build_object('id',v_row.id, 'task_id',v_row.task_id, 'project_id',v_row.project_id, 'video_segment_id',v_row.video_segment_id, 'user_id',v_row.user_id, 'source',v_row.source, 'annotation_type',v_row.annotation_type, 'tool_unit_id',v_row.tool_unit_id, 'class_name',v_row.class_name, 'geometry',v_row.geometry, 'confidence',v_row.confidence, 'parent_prediction_id',v_row.parent_prediction_id, 'parent_annotation_id',v_row.parent_annotation_id, 'track_id',v_row.track_id, 'scene_track_id',v_row.scene_track_id, 'sensor_dataset_item_id',v_row.sensor_dataset_item_id, 'sensor_role',v_row.sensor_role, 'sensor_visibility',v_row.sensor_visibility, 'calibration_revision',v_row.calibration_revision, 'calibration_digest',v_row.calibration_digest, 'temporal_role',v_row.temporal_role, 'lead_time',v_row.lead_time, 'was_cancelled',v_row.was_cancelled, 'ground_truth',v_row.ground_truth) || jsonb_build_object('is_active',v_row.is_active, 'attributes',v_row.attributes, 'attributes_meta',v_row.attributes_meta, 'z_order',v_row.z_order, 'is_locked',v_row.is_locked, 'is_hidden',v_row.is_hidden, 'version',v_row.version, 'created_at',v_row.created_at, 'updated_at',v_row.updated_at)));
        END LOOP;
        IF jsonb_array_length(v_annotations)<>cardinality(v_annotation_ids) THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_maintenance_error',DETAIL='{"reason":"annotation_target_changed"}';
        END IF;
        v_data := '[]'::jsonb;
        FOR v_item IN SELECT value FROM jsonb_array_elements(v_annotations) LOOP
            IF (v_item ->> 'is_active')::boolean THEN
                IF NOT (p_request -> 'class_names' ? (v_item ->> 'class_name')) THEN
                    v_orphans := v_orphans+1;
                    UPDATE public.annotations SET is_active=false,updated_at=transaction_timestamp()
                    WHERE id=(v_item ->> 'id')::uuid;
                    v_item := v_item || jsonb_build_object('is_active',false,'updated_at',transaction_timestamp());
                    v_affected := array_append(v_affected,(v_item ->> 'task_id')::uuid);
                    v_deactivated := array_append(v_deactivated,(v_item ->> 'task_id')::uuid);
                ELSIF jsonb_typeof(v_item -> 'attributes')='object' THEN
                    v_attributes := v_item -> 'attributes';
                    FOR v_field IN SELECT key FROM jsonb_object_keys(v_attributes) AS fields(key)
                        WHERE NOT (p_request -> 'attribute_keys' ? key) AND left(key,1)<>'_'
                    LOOP
                        v_attributes := v_attributes-v_field;
                        v_attribute_counts := jsonb_set(v_attribute_counts,ARRAY[v_field],
                            to_jsonb(COALESCE((v_attribute_counts ->> v_field)::integer,0)+1));
                    END LOOP;
                    IF v_attributes IS DISTINCT FROM v_item -> 'attributes' THEN
                        UPDATE public.annotations SET attributes=v_attributes,updated_at=transaction_timestamp()
                        WHERE id=(v_item ->> 'id')::uuid;
                        v_item := v_item || jsonb_build_object('attributes',v_attributes,'updated_at',transaction_timestamp());
                        v_affected := array_append(v_affected,(v_item ->> 'task_id')::uuid);
                    END IF;
                END IF;
            END IF;
            -- Preserve the pre-update ORM version when a Mask trigger advances only the stored revision.
            v_data := v_data || jsonb_build_array(v_item);
        END LOOP;
        v_data := jsonb_build_object('operation',v_operation,'annotations',v_data,'tasks',v_tasks,'batches',v_batches,
            'affected_task_ids',(SELECT COALESCE(jsonb_agg(DISTINCT id ORDER BY id),'[]'::jsonb) FROM unnest(v_affected) AS item(id)),
            'deactivated_task_ids',(SELECT COALESCE(jsonb_agg(DISTINCT id ORDER BY id),'[]'::jsonb) FROM unnest(v_deactivated) AS item(id)),
            'report',jsonb_build_object('orphan_annotations',v_orphans,'orphan_attribute_keys',v_attribute_counts));
    END IF;
    RETURN jsonb_build_object('schema','aap-labeling-annotation-maintenance/v1','ok',true,'request',p_request,'data',v_data);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_maintenance_error',DETAIL=jsonb_build_object('reason',v_phase)::text;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.annotation_maintenance_command(jsonb) FROM PUBLIC;
