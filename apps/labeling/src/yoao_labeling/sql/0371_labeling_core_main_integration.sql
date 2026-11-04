-- Extend published rows without changing existing column positions or grants.
CREATE OR REPLACE VIEW aap_labeling_v1.task_rows
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
    created_at, updated_at, thumbnail_output_id, original_id
FROM public.tasks;
-- aap-statement-break
CREATE OR REPLACE VIEW aap_labeling_v1.failed_prediction_rows
WITH (security_barrier = true)
AS
SELECT
    id, task_id, project_id, ml_backend_id, ml_backend_pool_id, model_version,
    error_type, message, retry_count, last_retry_at, dismissed_at, extra, created_at, execution_snapshot_id
FROM public.failed_predictions;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.lock_task_for_core_read(
    p_task_id uuid,
    p_lock_mode text DEFAULT 'update',
    p_nowait boolean DEFAULT false,
    p_project_id uuid DEFAULT NULL
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
        RETURN QUERY SELECT task.id, task.project_id, task.dataset_item_id, task.display_id, task.file_name, task.file_path, task.file_type, task.media_source_generation, task.media_object_identity, task.media_source_drifted, task.tags, task.status, task.assignee_id, task.assignee_is_override, task.assigned_at, task.is_labeled, task.overlap, task.total_annotations, task.total_predictions, task.precomputed_agreement, task.sequence_order, task.thumbnail_path, task.blurhash, task.batch_id, task.version, task.submitted_at, task.reviewer_id, task.reviewer_is_override, task.reviewer_claimed_at, task.review_round_id, task.first_review_eligible, task.first_reviewed_at, task.first_review_result, task.first_review_contributor_ids, task.annotation_contributor_ids, task.review_contributor_ids, task.review_submitter_id, task.reviewed_at, task.reject_reason, task.reject_reason_type, task.skip_reason, task.skipped_at, task.reopened_count, task.last_reopened_at, task.created_at, task.updated_at, task.thumbnail_output_id, task.original_id FROM public.tasks AS task
        WHERE task.id = p_task_id
          AND (p_project_id IS NULL OR task.project_id = p_project_id)
        FOR UPDATE NOWAIT;
    ELSIF p_lock_mode = 'update' THEN
        RETURN QUERY SELECT task.id, task.project_id, task.dataset_item_id, task.display_id, task.file_name, task.file_path, task.file_type, task.media_source_generation, task.media_object_identity, task.media_source_drifted, task.tags, task.status, task.assignee_id, task.assignee_is_override, task.assigned_at, task.is_labeled, task.overlap, task.total_annotations, task.total_predictions, task.precomputed_agreement, task.sequence_order, task.thumbnail_path, task.blurhash, task.batch_id, task.version, task.submitted_at, task.reviewer_id, task.reviewer_is_override, task.reviewer_claimed_at, task.review_round_id, task.first_review_eligible, task.first_reviewed_at, task.first_review_result, task.first_review_contributor_ids, task.annotation_contributor_ids, task.review_contributor_ids, task.review_submitter_id, task.reviewed_at, task.reject_reason, task.reject_reason_type, task.skip_reason, task.skipped_at, task.reopened_count, task.last_reopened_at, task.created_at, task.updated_at, task.thumbnail_output_id, task.original_id FROM public.tasks AS task
        WHERE task.id = p_task_id
          AND (p_project_id IS NULL OR task.project_id = p_project_id)
        FOR UPDATE;
    ELSIF p_lock_mode = 'share' AND p_nowait THEN
        RETURN QUERY SELECT task.id, task.project_id, task.dataset_item_id, task.display_id, task.file_name, task.file_path, task.file_type, task.media_source_generation, task.media_object_identity, task.media_source_drifted, task.tags, task.status, task.assignee_id, task.assignee_is_override, task.assigned_at, task.is_labeled, task.overlap, task.total_annotations, task.total_predictions, task.precomputed_agreement, task.sequence_order, task.thumbnail_path, task.blurhash, task.batch_id, task.version, task.submitted_at, task.reviewer_id, task.reviewer_is_override, task.reviewer_claimed_at, task.review_round_id, task.first_review_eligible, task.first_reviewed_at, task.first_review_result, task.first_review_contributor_ids, task.annotation_contributor_ids, task.review_contributor_ids, task.review_submitter_id, task.reviewed_at, task.reject_reason, task.reject_reason_type, task.skip_reason, task.skipped_at, task.reopened_count, task.last_reopened_at, task.created_at, task.updated_at, task.thumbnail_output_id, task.original_id FROM public.tasks AS task
        WHERE task.id = p_task_id
          AND (p_project_id IS NULL OR task.project_id = p_project_id)
        FOR SHARE NOWAIT;
    ELSIF p_lock_mode = 'share' THEN
        RETURN QUERY SELECT task.id, task.project_id, task.dataset_item_id, task.display_id, task.file_name, task.file_path, task.file_type, task.media_source_generation, task.media_object_identity, task.media_source_drifted, task.tags, task.status, task.assignee_id, task.assignee_is_override, task.assigned_at, task.is_labeled, task.overlap, task.total_annotations, task.total_predictions, task.precomputed_agreement, task.sequence_order, task.thumbnail_path, task.blurhash, task.batch_id, task.version, task.submitted_at, task.reviewer_id, task.reviewer_is_override, task.reviewer_claimed_at, task.review_round_id, task.first_review_eligible, task.first_reviewed_at, task.first_review_result, task.first_review_contributor_ids, task.annotation_contributor_ids, task.review_contributor_ids, task.review_submitter_id, task.reviewed_at, task.reject_reason, task.reject_reason_type, task.skip_reason, task.skipped_at, task.reopened_count, task.last_reopened_at, task.created_at, task.updated_at, task.thumbnail_output_id, task.original_id FROM public.tasks AS task
        WHERE task.id = p_task_id
          AND (p_project_id IS NULL OR task.project_id = p_project_id)
        FOR SHARE;
    ELSE
        RAISE EXCEPTION 'Invalid Labeling core-read lock mode'
            USING ERRCODE = '22023';
    END IF;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.lock_task_rows_for_core_read(
    p_task_ids uuid[],
    p_project_id uuid DEFAULT NULL,
    p_batch_id uuid DEFAULT NULL
)
RETURNS SETOF aap_labeling_v1.task_rows
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    RETURN QUERY
    SELECT
        task.id, task.project_id, task.dataset_item_id, task.display_id,
        task.file_name, task.file_path, task.file_type,
        task.media_source_generation, task.media_object_identity,
        task.media_source_drifted, task.tags, task.status, task.assignee_id,
        task.assignee_is_override, task.assigned_at, task.is_labeled,
        task.overlap, task.total_annotations, task.total_predictions,
        task.precomputed_agreement, task.sequence_order, task.thumbnail_path,
        task.blurhash, task.batch_id, task.version, task.submitted_at,
        task.reviewer_id, task.reviewer_is_override, task.reviewer_claimed_at,
        task.review_round_id, task.first_review_eligible, task.first_reviewed_at,
        task.first_review_result, task.first_review_contributor_ids,
        task.annotation_contributor_ids, task.review_contributor_ids,
        task.review_submitter_id, task.reviewed_at, task.reject_reason,
        task.reject_reason_type, task.skip_reason, task.skipped_at,
        task.reopened_count, task.last_reopened_at, task.created_at, task.updated_at, task.thumbnail_output_id, task.original_id
    FROM public.tasks AS task
    WHERE task.id = ANY(p_task_ids)
      AND (p_project_id IS NULL OR task.project_id = p_project_id)
      AND (p_batch_id IS NULL OR task.batch_id = p_batch_id)
    ORDER BY task.id
    FOR UPDATE NOWAIT;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.lock_active_learning_feedback_task_rows(
    p_project_id uuid,
    p_task_ids uuid[]
)
RETURNS SETOF aap_labeling_v1.task_rows
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF p_project_id IS NULL OR p_task_ids IS NULL THEN
        RAISE EXCEPTION 'invalid Labeling feedback task lock request'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT task.id, task.project_id, task.dataset_item_id, task.display_id,
           task.file_name, task.file_path, task.file_type,
           task.media_source_generation, task.media_object_identity,
           task.media_source_drifted, task.tags, task.status, task.assignee_id,
           task.assignee_is_override, task.assigned_at, task.is_labeled,
           task.overlap, task.total_annotations, task.total_predictions,
           task.precomputed_agreement, task.sequence_order, task.thumbnail_path,
           task.blurhash, task.batch_id, task.version, task.submitted_at,
           task.reviewer_id, task.reviewer_is_override, task.reviewer_claimed_at,
           task.review_round_id, task.first_review_eligible, task.first_reviewed_at,
           task.first_review_result, task.first_review_contributor_ids,
           task.annotation_contributor_ids, task.review_contributor_ids,
           task.review_submitter_id, task.reviewed_at, task.reject_reason,
           task.reject_reason_type, task.skip_reason, task.skipped_at,
           task.reopened_count, task.last_reopened_at, task.created_at,
           task.updated_at, task.thumbnail_output_id, task.original_id
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
      AND task.id = ANY (p_task_ids)
    ORDER BY task.id
    FOR SHARE;
END;
$function$;
