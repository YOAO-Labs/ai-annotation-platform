CREATE VIEW aap_labeling_v1.annotation_draft_rows
WITH (security_barrier = true)
AS
SELECT id, task_id
FROM public.annotation_drafts;
-- aap-statement-break
REVOKE ALL ON TABLE aap_labeling_v1.annotation_draft_rows FROM PUBLIC;
-- aap-statement-break
CREATE VIEW aap_assets_v1.labeling_dataset_item_facts
WITH (security_barrier = true)
AS
SELECT id, dataset_id, file_path, file_type, file_size, content_hash, width, height
FROM public.dataset_items;
-- aap-statement-break
REVOKE ALL ON TABLE aap_assets_v1.labeling_dataset_item_facts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.lock_active_learning_feedback_task_rows(
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
           task.updated_at
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
      AND task.id = ANY (p_task_ids)
    ORDER BY task.id
    FOR SHARE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.lock_active_learning_feedback_task_rows(uuid, uuid[])
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.lock_active_learning_feedback_annotation_rows(
    p_task_ids uuid[]
)
RETURNS SETOF aap_labeling_v1.annotation_rows
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF p_task_ids IS NULL THEN
        RAISE EXCEPTION 'invalid Labeling feedback annotation lock request'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT annotation.id, annotation.task_id, annotation.project_id,
           annotation.video_segment_id, annotation.user_id, annotation.source,
           annotation.annotation_type, annotation.tool_unit_id,
           annotation.class_name, annotation.geometry, annotation.confidence,
           annotation.parent_prediction_id, annotation.parent_annotation_id,
           annotation.track_id, annotation.scene_track_id,
           annotation.sensor_dataset_item_id, annotation.sensor_role,
           annotation.sensor_visibility, annotation.calibration_revision,
           annotation.calibration_digest, annotation.temporal_role,
           annotation.lead_time, annotation.was_cancelled, annotation.ground_truth,
           annotation.is_active, annotation.attributes, annotation.attributes_meta,
           annotation.z_order, annotation.is_locked, annotation.is_hidden,
           annotation.version, annotation.created_at, annotation.updated_at
    FROM public.annotations AS annotation
    WHERE annotation.task_id = ANY (p_task_ids)
      AND annotation.is_active IS TRUE
      AND annotation.was_cancelled IS FALSE
    ORDER BY annotation.id
    LIMIT 4097
    FOR SHARE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.next_batch_display_id()
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
    SELECT 'BT-' || pg_catalog.nextval(
        'public.display_seq_batches'::pg_catalog.regclass
    )::pg_catalog.text
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.next_batch_display_id() FROM PUBLIC;
