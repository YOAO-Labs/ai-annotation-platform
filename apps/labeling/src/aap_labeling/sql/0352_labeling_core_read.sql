CREATE VIEW aap_labeling_v1.task_rows
WITH (security_barrier = true)
AS
SELECT
    id, project_id, dataset_item_id, display_id, file_name, file_path, file_type,
    media_source_generation, media_object_identity, media_source_drifted, tags,
    status, assignee_id, assignee_is_override, assigned_at, is_labeled, overlap,
    total_annotations, total_predictions, precomputed_agreement, sequence_order,
    thumbnail_path, blurhash, batch_id, version, submitted_at, reviewer_id,
    reviewer_is_override, reviewer_claimed_at, review_round_id,
    first_review_eligible, first_reviewed_at, first_review_result,
    first_review_contributor_ids, annotation_contributor_ids,
    review_contributor_ids, review_submitter_id, reviewed_at, reject_reason,
    reject_reason_type, skip_reason, skipped_at, reopened_count, last_reopened_at,
    created_at, updated_at
FROM public.tasks;
-- aap-statement-break
CREATE VIEW aap_labeling_v1.batch_rows
WITH (security_barrier = true)
AS
SELECT
    id, project_id, dataset_id, display_id, name, description, status, priority,
    deadline, assigned_user_ids, total_tasks, completed_tasks, review_tasks,
    approved_tasks, rejected_tasks, created_by, created_at, updated_at,
    review_feedback, reviewed_at, reviewed_by, annotator_id, reviewer_id,
    admin_locked, admin_lock_reason, admin_locked_at, admin_locked_by
FROM public.task_batches;
-- aap-statement-break
CREATE VIEW aap_labeling_v1.annotation_rows
WITH (security_barrier = true)
AS
SELECT
    id, task_id, project_id, video_segment_id, user_id, source, annotation_type,
    tool_unit_id, class_name, geometry, confidence, parent_prediction_id,
    parent_annotation_id, track_id, scene_track_id, sensor_dataset_item_id,
    sensor_role, sensor_visibility, calibration_revision, calibration_digest,
    temporal_role, lead_time, was_cancelled, ground_truth, is_active, attributes,
    attributes_meta, z_order, is_locked, is_hidden, version, created_at, updated_at
FROM public.annotations;
-- aap-statement-break
CREATE VIEW aap_labeling_v1.prediction_rows
WITH (security_barrier = true)
AS
SELECT
    id, task_id, project_id, ml_backend_id, ml_backend_pool_id, model_version,
    score, tool_unit_id, result, cluster, mislabeling, source,
    rejected_shape_indexes, created_at
FROM public.predictions;
-- aap-statement-break
CREATE VIEW aap_labeling_v1.prediction_meta_rows
WITH (security_barrier = true)
AS
SELECT
    id, prediction_id, prediction_created_at, failed_prediction_id,
    inference_time_ms, prompt_tokens, completion_tokens, total_tokens,
    prompt_cost, completion_cost, total_cost, extra, created_at
FROM public.prediction_metas;
-- aap-statement-break
CREATE VIEW aap_labeling_v1.failed_prediction_rows
WITH (security_barrier = true)
AS
SELECT
    id, task_id, project_id, ml_backend_id, ml_backend_pool_id, model_version,
    error_type, message, retry_count, last_retry_at, dismissed_at, extra, created_at
FROM public.failed_predictions;
-- aap-statement-break
CREATE VIEW aap_labeling_v1.task_lock_rows
WITH (security_barrier = true)
AS
SELECT id, task_id, user_id, expire_at, unique_id, created_at
FROM public.task_locks;
-- aap-statement-break
CREATE VIEW aap_labeling_v1.task_dataset_item_link_rows
WITH (security_barrier = true)
AS
SELECT id, task_id, dataset_item_id, role, sensor_name, created_at
FROM public.task_dataset_item_links;
-- aap-statement-break
REVOKE ALL ON TABLE
    aap_labeling_v1.task_rows,
    aap_labeling_v1.batch_rows,
    aap_labeling_v1.annotation_rows,
    aap_labeling_v1.prediction_rows,
    aap_labeling_v1.prediction_meta_rows,
    aap_labeling_v1.failed_prediction_rows,
    aap_labeling_v1.task_lock_rows,
    aap_labeling_v1.task_dataset_item_link_rows
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.lock_task_for_core_read(
    p_task_id uuid,
    p_lock_mode text DEFAULT 'update',
    p_nowait boolean DEFAULT false
)
RETURNS SETOF aap_labeling_v1.task_rows
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF p_lock_mode IS NULL OR p_lock_mode NOT IN ('update', 'share')
       OR p_nowait IS NULL THEN
        RAISE EXCEPTION 'Invalid Labeling core-read lock mode'
            USING ERRCODE = '22023';
    END IF;
    IF p_lock_mode = 'update' AND p_nowait THEN
        RETURN QUERY SELECT task.* FROM public.tasks AS task
        WHERE task.id = p_task_id FOR UPDATE NOWAIT;
    ELSIF p_lock_mode = 'update' THEN
        RETURN QUERY SELECT task.* FROM public.tasks AS task
        WHERE task.id = p_task_id FOR UPDATE;
    ELSIF p_lock_mode = 'share' AND p_nowait THEN
        RETURN QUERY SELECT task.* FROM public.tasks AS task
        WHERE task.id = p_task_id FOR SHARE NOWAIT;
    ELSIF p_lock_mode = 'share' THEN
        RETURN QUERY SELECT task.* FROM public.tasks AS task
        WHERE task.id = p_task_id FOR SHARE;
    ELSE
        RAISE EXCEPTION 'Invalid Labeling core-read lock mode'
            USING ERRCODE = '22023';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.lock_task_for_core_read(uuid,text,boolean)
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.lock_batch_rows_for_core_read(
    p_batch_ids uuid[],
    p_project_id uuid,
    p_lock_mode text,
    p_nowait boolean
)
RETURNS SETOF aap_labeling_v1.batch_rows
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF p_lock_mode IS NULL OR p_lock_mode NOT IN ('update', 'share')
       OR p_nowait IS NULL THEN
        RAISE EXCEPTION 'Invalid Labeling core-read lock mode'
            USING ERRCODE = '22023';
    END IF;
    IF p_lock_mode = 'update' AND p_nowait THEN
        RETURN QUERY
        SELECT batch.*
        FROM public.task_batches AS batch
        WHERE batch.id = ANY(p_batch_ids)
          AND (p_project_id IS NULL OR batch.project_id = p_project_id)
        ORDER BY batch.id
        FOR UPDATE NOWAIT;
    ELSIF p_lock_mode = 'update' THEN
        RETURN QUERY
        SELECT batch.*
        FROM public.task_batches AS batch
        WHERE batch.id = ANY(p_batch_ids)
          AND (p_project_id IS NULL OR batch.project_id = p_project_id)
        ORDER BY batch.id
        FOR UPDATE;
    ELSIF p_lock_mode = 'share' AND p_nowait THEN
        RETURN QUERY
        SELECT batch.*
        FROM public.task_batches AS batch
        WHERE batch.id = ANY(p_batch_ids)
          AND (p_project_id IS NULL OR batch.project_id = p_project_id)
        ORDER BY batch.id
        FOR SHARE NOWAIT;
    ELSIF p_lock_mode = 'share' THEN
        RETURN QUERY
        SELECT batch.*
        FROM public.task_batches AS batch
        WHERE batch.id = ANY(p_batch_ids)
          AND (p_project_id IS NULL OR batch.project_id = p_project_id)
        ORDER BY batch.id
        FOR SHARE;
    ELSE
        RAISE EXCEPTION 'Invalid Labeling core-read lock mode'
            USING ERRCODE = '22023';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean)
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.lock_annotation_rows_for_core_read(
    p_annotation_ids uuid[],
    p_task_id uuid,
    p_project_id uuid,
    p_lock_mode text,
    p_nowait boolean
)
RETURNS SETOF aap_labeling_v1.annotation_rows
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF p_lock_mode IS NULL OR p_lock_mode NOT IN ('update', 'share')
       OR p_nowait IS NULL THEN
        RAISE EXCEPTION 'Invalid Labeling core-read lock mode'
            USING ERRCODE = '22023';
    END IF;
    IF p_lock_mode = 'update' AND p_nowait THEN
        RETURN QUERY SELECT annotation.* FROM public.annotations AS annotation
        WHERE annotation.id = ANY(p_annotation_ids)
          AND (p_task_id IS NULL OR annotation.task_id = p_task_id)
          AND (p_project_id IS NULL OR annotation.project_id = p_project_id)
        ORDER BY annotation.id FOR UPDATE NOWAIT;
    ELSIF p_lock_mode = 'update' THEN
        RETURN QUERY SELECT annotation.* FROM public.annotations AS annotation
        WHERE annotation.id = ANY(p_annotation_ids)
          AND (p_task_id IS NULL OR annotation.task_id = p_task_id)
          AND (p_project_id IS NULL OR annotation.project_id = p_project_id)
        ORDER BY annotation.id FOR UPDATE;
    ELSIF p_lock_mode = 'share' AND p_nowait THEN
        RETURN QUERY SELECT annotation.* FROM public.annotations AS annotation
        WHERE annotation.id = ANY(p_annotation_ids)
          AND (p_task_id IS NULL OR annotation.task_id = p_task_id)
          AND (p_project_id IS NULL OR annotation.project_id = p_project_id)
        ORDER BY annotation.id FOR SHARE NOWAIT;
    ELSIF p_lock_mode = 'share' THEN
        RETURN QUERY SELECT annotation.* FROM public.annotations AS annotation
        WHERE annotation.id = ANY(p_annotation_ids)
          AND (p_task_id IS NULL OR annotation.task_id = p_task_id)
          AND (p_project_id IS NULL OR annotation.project_id = p_project_id)
        ORDER BY annotation.id FOR SHARE;
    ELSE
        RAISE EXCEPTION 'Invalid Labeling core-read lock mode'
            USING ERRCODE = '22023';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean)
FROM PUBLIC;
