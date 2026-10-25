CREATE FUNCTION aap_labeling_v1.spatial_quality_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_action text;
    v_allowed_keys text[];
    v_required_keys text[];
    v_data jsonb := '{}'::jsonb;
    v_row record;
    v_run public.point_cloud_quality_runs%ROWTYPE;
    v_issue public.point_cloud_quality_issues%ROWTYPE;
    v_evaluation public.point_cloud_quality_evaluations%ROWTYPE;
    v_video_run public.video_track_quality_runs%ROWTYPE;
    v_item jsonb;
    v_now timestamptz;
    v_status text;
    v_rows jsonb;
    v_total bigint;
    v_filter_status text;
    v_limit integer;
    v_offset integer;
BEGIN
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) <> 'object'
       OR pg_catalog.jsonb_typeof(p_request -> 'schema') <> 'string'
       OR p_request ->> 'schema' <> 'aap-labeling-spatial-quality/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'action') <> 'string' THEN
        RAISE EXCEPTION 'invalid Labeling spatial quality request'
            USING ERRCODE = '22023';
    END IF;

    v_action := p_request ->> 'action';
    CASE v_action
        WHEN 'read_point_source' THEN
            v_allowed_keys := ARRAY['schema','action','project_id'];
            v_required_keys := v_allowed_keys;
        WHEN 'read_point_details' THEN
            v_allowed_keys := ARRAY['schema','action','project_id','task_ids','scene_ids','track_ids','annotation_ids','annotation_scope'];
            v_required_keys := v_allowed_keys;
        WHEN 'read_quality_facts' THEN
            v_allowed_keys := ARRAY['schema','action','project_id','annotation_ids','task_ids','track_ids','run_ids'];
            v_required_keys := ARRAY['schema','action','annotation_ids','task_ids','track_ids','run_ids'];
        WHEN 'read_video_annotations' THEN
            v_allowed_keys := ARRAY['schema','action','task_id','segment_ids'];
            v_required_keys := v_allowed_keys;
        WHEN 'read_video_task_annotations' THEN
            v_allowed_keys := ARRAY['schema','action','task_id'];
            v_required_keys := v_allowed_keys;
        WHEN 'read_video_boundary' THEN
            v_allowed_keys := ARRAY['schema','action','task_id','dataset_item_id','left_segment_id','right_segment_id'];
            v_required_keys := v_allowed_keys;
        WHEN 'read_video_task_segments' THEN
            v_allowed_keys := ARRAY['schema','action','task_id'];
            v_required_keys := v_allowed_keys;
        WHEN 'find_point_run' THEN
            v_allowed_keys := ARRAY['schema','action','project_id','singleflight_key','statuses'];
            v_required_keys := v_allowed_keys;
        WHEN 'create_point_run' THEN
            v_allowed_keys := ARRAY['schema','action','new_run_id','project_id','async_job_id','actor_id','scope_json','config_revision','config_digest','config_snapshot','source_snapshot','source_snapshot_digest','singleflight_key','summary'];
            v_required_keys := v_allowed_keys;
        WHEN 'get_point_run' THEN
            v_allowed_keys := ARRAY['schema','action','run_id'];
            v_required_keys := v_allowed_keys;
        WHEN 'update_point_run' THEN
            v_allowed_keys := ARRAY['schema','action','run_id','status','progress_pct','summary','error_message','started_at','completed_at'];
            v_required_keys := ARRAY['schema','action','run_id','status'];
        WHEN 'reconcile_point_run' THEN
            v_allowed_keys := ARRAY['schema','action','async_job_id','status','completed_at','error_message'];
            v_required_keys := ARRAY['schema','action','async_job_id','status','completed_at'];
        WHEN 'upsert_point_issue' THEN
            v_allowed_keys := ARRAY['schema','action','new_issue_id','run_id','project_id','scene_id','task_id','annotation_id','annotation_version','scene_track_id','track_revision','related_annotation_ids','source_versions','class_name','code','rule_version','severity','severity_rank','frame_start','frame_end','metric','threshold','evidence','locator','suggested_command','dedupe_key'];
            v_required_keys := ARRAY['schema','action','new_issue_id','run_id','project_id','scene_id','related_annotation_ids','source_versions','code','rule_version','severity','severity_rank','metric','threshold','evidence','locator','dedupe_key'];
        WHEN 'get_point_issue', 'lock_point_issue' THEN
            v_allowed_keys := ARRAY['schema','action','issue_id'];
            v_required_keys := v_allowed_keys;
        WHEN 'list_point_issues' THEN
            v_allowed_keys := ARRAY['schema','action','project_id','review_status','limit','offset','severity_filter','code_filter','scene_filter','task_filter','annotation_filter','track_filter','frame_filter','allowed_task_ids'];
            v_required_keys := ARRAY['schema','action','project_id','limit','offset'];
        WHEN 'mark_point_issues_stale' THEN
            v_allowed_keys := ARRAY['schema','action','stale_issue_ids'];
            v_required_keys := v_allowed_keys;
        WHEN 'decide_point_issue' THEN
            v_allowed_keys := ARRAY['schema','action','issue_id','actor_id','review_status','expected_task_id','resolution_reason','review_verdict','review_note','resolved_at','reviewed_at'];
            v_required_keys := ARRAY['schema','action','issue_id','actor_id','review_status','expected_task_id','resolved_at'];
        WHEN 'list_point_evaluation_samples' THEN
            v_allowed_keys := ARRAY['schema','action','project_id','cutoff_at','limit'];
            v_required_keys := v_allowed_keys;
        WHEN 'create_point_evaluation' THEN
            v_allowed_keys := ARRAY['schema','action','new_evaluation_id','project_id','actor_id','baseline_config_revision','baseline_config_digest','baseline_config_snapshot','candidate_config_digest','candidate_config_snapshot','cutoff_at','sample_count','sample_snapshot','summary','gate_status','gate_reasons'];
            v_required_keys := v_allowed_keys;
        WHEN 'get_point_evaluation' THEN
            v_allowed_keys := ARRAY['schema','action','evaluation_id'];
            v_required_keys := v_allowed_keys;
        WHEN 'list_point_evaluations' THEN
            v_allowed_keys := ARRAY['schema','action','project_id','limit','offset'];
            v_required_keys := v_allowed_keys;
        WHEN 'promote_point_evaluation' THEN
            v_allowed_keys := ARRAY['schema','action','evaluation_id','actor_id','promoted_config_revision','promoted_at'];
            v_required_keys := v_allowed_keys;
        WHEN 'find_video_run' THEN
            v_allowed_keys := ARRAY['schema','action','task_id','left_segment_id','right_segment_id','input_digest','statuses'];
            v_required_keys := v_allowed_keys;
        WHEN 'create_video_run' THEN
            v_allowed_keys := ARRAY['schema','action','new_run_id','task_id','left_segment_id','right_segment_id','async_job_id','actor_id','input_snapshot','input_digest','sampling_digest','metrics'];
            v_required_keys := v_allowed_keys;
        WHEN 'get_video_run', 'lock_video_run' THEN
            v_allowed_keys := ARRAY['schema','action','run_id'];
            v_required_keys := v_allowed_keys;
        WHEN 'list_video_runs' THEN
            v_allowed_keys := ARRAY['schema','action','task_id'];
            v_required_keys := v_allowed_keys;
        WHEN 'list_accepted_video_runs' THEN
            v_allowed_keys := ARRAY['schema','action','task_id','run_id'];
            v_required_keys := v_allowed_keys;
        WHEN 'update_video_run' THEN
            v_allowed_keys := ARRAY['schema','action','run_id','status','progress_pct','error_message','started_at','completed_at','stale_at','accepted_by_id','accepted_at','metrics','pairs'];
            v_required_keys := ARRAY['schema','action','run_id','status'];
        WHEN 'replace_video_issues' THEN
            v_allowed_keys := ARRAY['schema','action','run_id','issue_rows'];
            v_required_keys := v_allowed_keys;
        WHEN 'list_video_issues' THEN
            v_allowed_keys := ARRAY['schema','action','run_id'];
            v_required_keys := v_allowed_keys;
        WHEN 'mark_video_runs_stale' THEN
            v_allowed_keys := ARRAY['schema','action','segment_ids'];
            v_required_keys := v_allowed_keys;
        ELSE
            RAISE EXCEPTION 'invalid Labeling spatial quality request'
                USING ERRCODE = '22023';
    END CASE;

    IF NOT (p_request ?& v_required_keys)
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_object_keys(p_request) AS input_keys(key)
            WHERE NOT (input_keys.key = ANY(v_allowed_keys))
       ) THEN
        RAISE EXCEPTION 'invalid Labeling spatial quality request'
            USING ERRCODE = '22023';
    END IF;

    CASE v_action
        WHEN 'read_point_source' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_data := pg_catalog.jsonb_build_object(
                'tasks', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'id', task_row.id,
                        'project_id', task_row.project_id,
                        'version', task_row.version,
                        'dataset_item_id', task_row.dataset_item_id
                    ) ORDER BY task_row.id)
                    FROM public.tasks AS task_row
                    WHERE task_row.project_id = (p_request ->> 'project_id')::uuid
                ), '[]'::jsonb),
                'track_scene_ids', COALESCE((
                    SELECT pg_catalog.jsonb_agg(scene_ids.scene_id ORDER BY scene_ids.scene_id)
                    FROM (
                        SELECT DISTINCT track_row.scene_id
                        FROM public.scene_tracks AS track_row
                        WHERE track_row.project_id = (p_request ->> 'project_id')::uuid
                    ) AS scene_ids
                ), '[]'::jsonb),
                'dataset_ids', COALESCE((
                    SELECT pg_catalog.jsonb_agg(dataset.dataset_id ORDER BY dataset.dataset_id)
                    FROM aap_assets_v1.workspace_project_query_dataset_ids(
                        (p_request ->> 'project_id')::uuid
                    ) AS dataset
                ), '[]'::jsonb)
            );
        WHEN 'read_point_details' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'task_ids') <> 'array'
               OR pg_catalog.jsonb_typeof(p_request -> 'scene_ids') <> 'array'
               OR pg_catalog.jsonb_typeof(p_request -> 'track_ids') <> 'array'
               OR pg_catalog.jsonb_typeof(p_request -> 'annotation_ids') <> 'array'
               OR pg_catalog.jsonb_typeof(p_request -> 'annotation_scope') <> 'string'
               OR p_request ->> 'annotation_scope' NOT IN ('task_ids','annotation_ids','all')
               OR pg_catalog.jsonb_array_length(p_request -> 'task_ids') > 5000
               OR pg_catalog.jsonb_array_length(p_request -> 'scene_ids') > 5000
               OR pg_catalog.jsonb_array_length(p_request -> 'track_ids') > 20000
               OR pg_catalog.jsonb_array_length(p_request -> 'annotation_ids') > 5000 THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_data := pg_catalog.jsonb_build_object(
                'annotations', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'id', annotation_row.id,
                        'project_id', annotation_row.project_id,
                        'task_id', annotation_row.task_id,
                        'annotation_type', annotation_row.annotation_type,
                        'is_active', annotation_row.is_active,
                        'was_cancelled', annotation_row.was_cancelled,
                        'version', annotation_row.version,
                        'class_name', annotation_row.class_name,
                        'track_id', annotation_row.track_id,
                        'scene_track_id', annotation_row.scene_track_id,
                        'geometry', annotation_row.geometry
                    ) ORDER BY annotation_row.id)
                    FROM public.annotations AS annotation_row
                    WHERE annotation_row.project_id = (p_request ->> 'project_id')::uuid
                      AND annotation_row.annotation_type = 'box_3d'
                      AND annotation_row.is_active
                      AND NOT annotation_row.was_cancelled
                      AND CASE p_request ->> 'annotation_scope'
                          WHEN 'annotation_ids' THEN annotation_row.id = ANY(ARRAY(
                              SELECT value::uuid
                              FROM pg_catalog.jsonb_array_elements_text(p_request -> 'annotation_ids') AS values(value)
                          ))
                          ELSE annotation_row.task_id = ANY(ARRAY(
                              SELECT value::uuid
                              FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids') AS values(value)
                          ))
                      END
                ), '[]'::jsonb),
                'camera_annotations', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'id', camera_row.id,
                        'project_id', camera_row.project_id,
                        'task_id', camera_row.task_id,
                        'sensor_role', camera_row.sensor_role,
                        'is_active', camera_row.is_active,
                        'was_cancelled', camera_row.was_cancelled,
                        'scene_track_id', camera_row.scene_track_id,
                        'class_name', camera_row.class_name,
                        'track_id', camera_row.track_id,
                        'sensor_dataset_item_id', camera_row.sensor_dataset_item_id,
                        'sensor_visibility', camera_row.sensor_visibility,
                        'calibration_revision', camera_row.calibration_revision,
                        'calibration_digest', camera_row.calibration_digest,
                        'geometry', camera_row.geometry
                    ) ORDER BY camera_row.id)
                    FROM public.annotations AS camera_row
                    WHERE camera_row.project_id = (p_request ->> 'project_id')::uuid
                      AND camera_row.sensor_role IS NOT NULL
                      AND camera_row.is_active
                      AND NOT camera_row.was_cancelled
                      AND camera_row.task_id = ANY(ARRAY(
                          SELECT value::uuid
                          FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids') AS values(value)
                      ))
                      AND (
                          p_request ->> 'annotation_scope' = 'all'
                          OR camera_row.scene_track_id = ANY(ARRAY(
                              SELECT value::uuid
                              FROM pg_catalog.jsonb_array_elements_text(p_request -> 'track_ids') AS values(value)
                          ))
                      )
                ), '[]'::jsonb),
                'tracks', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'id', track_row.id,
                        'project_id', track_row.project_id,
                        'scene_id', track_row.scene_id,
                        'track_id', track_row.track_id,
                        'class_name', track_row.class_name,
                        'presence_mode', track_row.presence_mode,
                        'revision', track_row.revision,
                        'retired_at', track_row.retired_at
                    ) ORDER BY track_row.id)
                    FROM public.scene_tracks AS track_row
                    WHERE track_row.project_id = (p_request ->> 'project_id')::uuid
                      AND track_row.scene_id = ANY(ARRAY(
                          SELECT value::uuid
                          FROM pg_catalog.jsonb_array_elements_text(p_request -> 'scene_ids') AS values(value)
                      ))
                      AND track_row.retired_at IS NULL
                      AND (
                          p_request ->> 'annotation_scope' = 'all'
                          OR track_row.id = ANY(ARRAY(
                              SELECT value::uuid
                              FROM pg_catalog.jsonb_array_elements_text(p_request -> 'track_ids') AS values(value)
                          ))
                      )
                ), '[]'::jsonb),
                'intervals', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'id', interval_row.id,
                        'scene_track_id', interval_row.scene_track_id,
                        'start_frame', interval_row.start_frame,
                        'end_frame', interval_row.end_frame,
                        'version', interval_row.version
                    ) ORDER BY interval_row.scene_track_id, interval_row.start_frame)
                    FROM public.scene_track_intervals AS interval_row
                    WHERE interval_row.scene_track_id IN (
                        SELECT track_row.id
                        FROM public.scene_tracks AS track_row
                        WHERE track_row.project_id = (p_request ->> 'project_id')::uuid
                          AND track_row.scene_id = ANY(ARRAY(
                              SELECT value::uuid
                              FROM pg_catalog.jsonb_array_elements_text(p_request -> 'scene_ids') AS values(value)
                          ))
                          AND track_row.retired_at IS NULL
                          AND (
                              p_request ->> 'annotation_scope' = 'all'
                              OR track_row.id = ANY(ARRAY(
                                  SELECT value::uuid
                                  FROM pg_catalog.jsonb_array_elements_text(p_request -> 'track_ids') AS values(value)
                              ))
                          )
                    )
                ), '[]'::jsonb),
                'links', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'task_id', link_row.task_id,
                        'dataset_item_id', link_row.dataset_item_id,
                        'role', link_row.role
                    ) ORDER BY link_row.task_id, link_row.role, link_row.dataset_item_id)
                    FROM public.task_dataset_item_links AS link_row
                    WHERE link_row.task_id = ANY(ARRAY(
                        SELECT value::uuid
                        FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids') AS values(value)
                    ))
                ), '[]'::jsonb)
            );
        WHEN 'read_quality_facts' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'annotation_ids') <> 'array'
               OR pg_catalog.jsonb_typeof(p_request -> 'task_ids') <> 'array'
               OR pg_catalog.jsonb_typeof(p_request -> 'track_ids') <> 'array'
               OR pg_catalog.jsonb_typeof(p_request -> 'run_ids') <> 'array'
               OR pg_catalog.jsonb_array_length(p_request -> 'annotation_ids') > 20000
               OR pg_catalog.jsonb_array_length(p_request -> 'task_ids') > 5000
               OR pg_catalog.jsonb_array_length(p_request -> 'track_ids') > 20000
               OR pg_catalog.jsonb_array_length(p_request -> 'run_ids') > 20000
               OR (p_request ? 'project_id' AND pg_catalog.jsonb_typeof(p_request -> 'project_id') NOT IN ('string','null')) THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_data := pg_catalog.jsonb_build_object(
                'annotations', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'id', row.id, 'task_id', row.task_id, 'project_id', row.project_id,
                        'version', row.version, 'is_active', row.is_active,
                        'was_cancelled', row.was_cancelled
                    ) ORDER BY row.id)
                    FROM public.annotations AS row
                    WHERE row.id = ANY(ARRAY(SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'annotation_ids') AS values(value)))
                      AND (NOT (p_request ? 'project_id') OR pg_catalog.jsonb_typeof(p_request -> 'project_id') = 'null' OR row.project_id = (p_request ->> 'project_id')::uuid)
                ), '[]'::jsonb),
                'tasks', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'id', row.id, 'project_id', row.project_id, 'dataset_item_id', row.dataset_item_id,
                        'version', row.version, 'file_type', row.file_type,
                        'status', row.status, 'batch_id', row.batch_id,
                        'assignee_id', row.assignee_id, 'reviewer_id', row.reviewer_id
                    ) ORDER BY row.id)
                    FROM public.tasks AS row
                    WHERE row.id = ANY(ARRAY(SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids') AS values(value)))
                      AND (NOT (p_request ? 'project_id') OR pg_catalog.jsonb_typeof(p_request -> 'project_id') = 'null' OR row.project_id = (p_request ->> 'project_id')::uuid)
                ), '[]'::jsonb),
                'tracks', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'id', row.id, 'scene_id', row.scene_id, 'revision', row.revision
                    ) ORDER BY row.id)
                    FROM public.scene_tracks AS row
                    WHERE row.id = ANY(ARRAY(SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'track_ids') AS values(value)))
                ), '[]'::jsonb),
                'links', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'task_id', row.task_id, 'dataset_item_id', row.dataset_item_id, 'role', row.role
                    ) ORDER BY row.task_id, row.role, row.dataset_item_id)
                    FROM public.task_dataset_item_links AS row
                    WHERE row.task_id = ANY(ARRAY(SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids') AS values(value)))
                ), '[]'::jsonb),
                'runs', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'id', row.id, 'project_id', row.project_id,
                        'config_digest', row.config_digest
                    ) ORDER BY row.id)
                    FROM public.point_cloud_quality_runs AS row
                    WHERE row.id = ANY(ARRAY(SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'run_ids') AS values(value)))
                ), '[]'::jsonb)
            );
        WHEN 'read_video_annotations' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'task_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'segment_ids') <> 'array'
               OR pg_catalog.jsonb_array_length(p_request -> 'segment_ids') NOT BETWEEN 1 AND 2 THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_data := pg_catalog.jsonb_build_object(
                'annotations', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'id', row.id, 'task_id', row.task_id, 'video_segment_id', row.video_segment_id,
                        'version', row.version, 'class_name', row.class_name, 'tool_unit_id', row.tool_unit_id,
                        'track_id', row.track_id, 'geometry', row.geometry
                    ) ORDER BY row.video_segment_id, row.id)
                    FROM public.annotations AS row
                    WHERE row.task_id = (p_request ->> 'task_id')::uuid
                      AND row.video_segment_id = ANY(ARRAY(SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'segment_ids') AS values(value)))
                      AND row.is_active AND NOT row.was_cancelled
                ), '[]'::jsonb)
            );
        WHEN 'read_video_task_annotations' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'task_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_data := pg_catalog.jsonb_build_object(
                'annotations', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'id', row.id, 'task_id', row.task_id, 'video_segment_id', row.video_segment_id,
                        'version', row.version, 'class_name', row.class_name, 'tool_unit_id', row.tool_unit_id,
                        'track_id', row.track_id, 'geometry', row.geometry
                    ) ORDER BY row.video_segment_id, row.id)
                    FROM public.annotations AS row
                    WHERE row.task_id = (p_request ->> 'task_id')::uuid
                      AND row.is_active AND NOT row.was_cancelled
                ), '[]'::jsonb)
            );
        WHEN 'read_video_boundary' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'task_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'dataset_item_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'left_segment_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'right_segment_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_data := pg_catalog.jsonb_build_object(
                'segments', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'id', row.id, 'dataset_item_id', row.dataset_item_id,
                        'segment_index', row.segment_index, 'start_frame', row.start_frame,
                        'end_frame', row.end_frame, 'status', row.status
                    ) ORDER BY row.segment_index)
                    FROM public.video_segments AS row
                    WHERE row.dataset_item_id = (p_request ->> 'dataset_item_id')::uuid
                      AND row.id = ANY(ARRAY[(p_request ->> 'left_segment_id')::uuid,(p_request ->> 'right_segment_id')::uuid])
                      AND EXISTS (
                          SELECT 1 FROM public.tasks AS task_row
                          WHERE task_row.id = (p_request ->> 'task_id')::uuid
                            AND task_row.dataset_item_id = row.dataset_item_id
                      )
                ), '[]'::jsonb),
                'segment_count', (
                    SELECT pg_catalog.count(*)
                    FROM public.video_segments AS row
                    WHERE row.dataset_item_id = (p_request ->> 'dataset_item_id')::uuid
                      AND EXISTS (
                          SELECT 1 FROM public.tasks AS task_row
                          WHERE task_row.id = (p_request ->> 'task_id')::uuid
                            AND task_row.dataset_item_id = row.dataset_item_id
                      )
                )
            );
        WHEN 'read_video_task_segments' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'task_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_data := pg_catalog.jsonb_build_object(
                'segments',COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'id',segment_row.id,'dataset_item_id',segment_row.dataset_item_id,
                        'segment_index',segment_row.segment_index,'start_frame',segment_row.start_frame,
                        'end_frame',segment_row.end_frame,'status',segment_row.status
                    ) ORDER BY segment_row.segment_index)
                    FROM public.video_segments AS segment_row
                    WHERE segment_row.id IN (
                        SELECT DISTINCT annotation_row.video_segment_id
                        FROM public.annotations AS annotation_row
                        WHERE annotation_row.task_id=(p_request ->> 'task_id')::uuid
                          AND annotation_row.video_segment_id IS NOT NULL
                          AND annotation_row.is_active
                          AND NOT annotation_row.was_cancelled
                    )
                ),'[]'::jsonb)
            );
        WHEN 'find_point_run' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'singleflight_key') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'statuses') <> 'array'
               OR pg_catalog.jsonb_array_length(p_request -> 'statuses') > 8
               OR EXISTS (
                    SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request -> 'statuses') AS values(value)
                    WHERE pg_catalog.jsonb_typeof(values.value) <> 'string'
                       OR values.value #>> '{}' NOT IN ('pending','running','completed','failed','cancelled','stale')
               ) THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            SELECT row.* INTO v_run
            FROM public.point_cloud_quality_runs AS row
            WHERE row.project_id = (p_request ->> 'project_id')::uuid
              AND row.singleflight_key = p_request ->> 'singleflight_key'
              AND row.status = ANY(ARRAY(SELECT value FROM pg_catalog.jsonb_array_elements_text(p_request -> 'statuses') AS values(value)))
            ORDER BY row.created_at DESC
            LIMIT 1;
            v_data := pg_catalog.jsonb_build_object('row', CASE WHEN v_run.id IS NULL THEN NULL ELSE pg_catalog.to_jsonb(v_run) END);
        WHEN 'create_point_run' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'new_run_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'async_job_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'actor_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'scope_json') <> 'object'
               OR pg_catalog.jsonb_typeof(p_request -> 'config_revision') <> 'number'
               OR (p_request ->> 'config_revision')::integer < 1
               OR (p_request ->> 'config_revision')::numeric <> pg_catalog.trunc((p_request ->> 'config_revision')::numeric)
               OR pg_catalog.jsonb_typeof(p_request -> 'config_digest') <> 'string'
               OR p_request ->> 'config_digest' !~ '^[0-9a-f]{64}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'config_snapshot') <> 'object'
               OR pg_catalog.jsonb_typeof(p_request -> 'source_snapshot') <> 'array'
               OR pg_catalog.jsonb_typeof(p_request -> 'source_snapshot_digest') <> 'string'
               OR p_request ->> 'source_snapshot_digest' !~ '^[0-9a-f]{64}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'singleflight_key') <> 'string'
               OR p_request ->> 'singleflight_key' !~ '^[0-9a-f]{64}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'summary') <> 'object' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.point_cloud_quality_runs (
                id, project_id, async_job_id, requested_by_id, status, progress_pct,
                scope_json, config_revision, config_digest, config_snapshot,
                source_snapshot, source_snapshot_digest, singleflight_key, summary
            ) VALUES (
                (p_request ->> 'new_run_id')::uuid,
                (p_request ->> 'project_id')::uuid,
                (p_request ->> 'async_job_id')::uuid,
                (p_request ->> 'actor_id')::uuid,
                'pending', 0,
                p_request -> 'scope_json',
                (p_request ->> 'config_revision')::integer,
                p_request ->> 'config_digest',
                p_request -> 'config_snapshot',
                p_request -> 'source_snapshot',
                p_request ->> 'source_snapshot_digest',
                p_request ->> 'singleflight_key',
                p_request -> 'summary'
            ) RETURNING * INTO v_run;
            v_data := pg_catalog.jsonb_build_object('row', pg_catalog.to_jsonb(v_run));
        WHEN 'get_point_run' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'run_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            SELECT row.* INTO v_run FROM public.point_cloud_quality_runs AS row WHERE row.id = (p_request ->> 'run_id')::uuid;
            v_data := pg_catalog.jsonb_build_object('row', CASE WHEN v_run.id IS NULL THEN NULL ELSE pg_catalog.to_jsonb(v_run) END);
        WHEN 'update_point_run' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'run_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'status') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_status := p_request ->> 'status';
            IF v_status = 'start' THEN
                IF pg_catalog.jsonb_object_length(p_request) <> 5 OR NOT (p_request ?& ARRAY['started_at']) OR pg_catalog.jsonb_typeof(p_request -> 'started_at') <> 'string' THEN
                    RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
                END IF;
                UPDATE public.point_cloud_quality_runs AS row
                SET status = 'running', progress_pct = 1, started_at = (p_request ->> 'started_at')::timestamptz,
                    updated_at = pg_catalog.transaction_timestamp()
                WHERE row.id = (p_request ->> 'run_id')::uuid
                RETURNING * INTO v_run;
            ELSIF v_status = 'progress' THEN
                IF pg_catalog.jsonb_object_length(p_request) <> 5 OR NOT (p_request ?& ARRAY['progress_pct']) OR pg_catalog.jsonb_typeof(p_request -> 'progress_pct') <> 'number' OR (p_request ->> 'progress_pct')::numeric NOT BETWEEN 0 AND 100 OR (p_request ->> 'progress_pct')::numeric <> pg_catalog.trunc((p_request ->> 'progress_pct')::numeric) THEN
                    RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
                END IF;
                UPDATE public.point_cloud_quality_runs AS row
                SET progress_pct = (p_request ->> 'progress_pct')::integer,
                    updated_at = pg_catalog.transaction_timestamp()
                WHERE row.id = (p_request ->> 'run_id')::uuid
                RETURNING * INTO v_run;
            ELSIF v_status = 'summary' THEN
                IF pg_catalog.jsonb_object_length(p_request) <> 5 OR NOT (p_request ? 'summary') OR pg_catalog.jsonb_typeof(p_request -> 'summary') <> 'object' THEN
                    RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
                END IF;
                UPDATE public.point_cloud_quality_runs AS row
                SET summary = p_request -> 'summary', updated_at = pg_catalog.transaction_timestamp()
                WHERE row.id = (p_request ->> 'run_id')::uuid
                RETURNING * INTO v_run;
            ELSIF v_status = 'completed' THEN
                IF pg_catalog.jsonb_object_length(p_request) <> 7 OR NOT (p_request ?& ARRAY['progress_pct','summary','completed_at']) OR pg_catalog.jsonb_typeof(p_request -> 'progress_pct') <> 'number' OR (p_request ->> 'progress_pct')::numeric NOT BETWEEN 0 AND 100 OR (p_request ->> 'progress_pct')::numeric <> pg_catalog.trunc((p_request ->> 'progress_pct')::numeric) OR pg_catalog.jsonb_typeof(p_request -> 'summary') <> 'object' OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') <> 'string' THEN
                    RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
                END IF;
                UPDATE public.point_cloud_quality_runs AS row
                SET status = 'completed', progress_pct = (p_request ->> 'progress_pct')::integer,
                    summary = p_request -> 'summary', completed_at = (p_request ->> 'completed_at')::timestamptz,
                    updated_at = pg_catalog.transaction_timestamp()
                WHERE row.id = (p_request ->> 'run_id')::uuid
                RETURNING * INTO v_run;
            ELSIF v_status IN ('stale','cancelled','failed') THEN
                IF NOT (p_request ? 'completed_at') OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') <> 'string'
                   OR (v_status IN ('stale','failed') AND (NOT (p_request ? 'error_message') OR pg_catalog.jsonb_typeof(p_request -> 'error_message') <> 'string'))
                   OR (v_status = 'cancelled' AND p_request ? 'error_message')
                   OR pg_catalog.jsonb_object_length(p_request) <> (CASE WHEN v_status = 'cancelled' THEN 5 ELSE 6 END) THEN
                    RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
                END IF;
                UPDATE public.point_cloud_quality_runs AS row
                SET status = v_status,
                    error_message = CASE WHEN v_status IN ('stale','failed') THEN left(p_request ->> 'error_message',4000) ELSE row.error_message END,
                    completed_at = (p_request ->> 'completed_at')::timestamptz,
                    updated_at = pg_catalog.transaction_timestamp()
                WHERE row.id = (p_request ->> 'run_id')::uuid
                RETURNING * INTO v_run;
            ELSE
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_data := pg_catalog.jsonb_build_object(
                'row', CASE WHEN v_run.id IS NULL THEN NULL ELSE pg_catalog.jsonb_build_object(
                    'id',v_run.id,'status',v_run.status,'progress_pct',v_run.progress_pct
                ) END
            );
        WHEN 'reconcile_point_run' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'status') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') <> 'string'
               OR p_request ->> 'status' NOT IN ('cancelled','failed')
               OR (p_request ->> 'status' = 'failed' AND pg_catalog.jsonb_typeof(p_request -> 'error_message') <> 'string')
               OR (p_request ->> 'status' = 'cancelled' AND p_request ? 'error_message') THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            UPDATE public.point_cloud_quality_runs AS row
            SET status = p_request ->> 'status',
                error_message = CASE WHEN p_request ->> 'status' = 'failed' THEN left(p_request ->> 'error_message',4000) ELSE row.error_message END,
                completed_at = (p_request ->> 'completed_at')::timestamptz,
                updated_at = pg_catalog.transaction_timestamp()
            WHERE row.async_job_id = (p_request ->> 'async_job_id')::uuid
              AND row.status IN ('pending','running')
            RETURNING * INTO v_run;
            v_data := pg_catalog.jsonb_build_object('changed',v_run.id IS NOT NULL);
        WHEN 'upsert_point_issue' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'new_issue_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'run_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'scene_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'related_annotation_ids') <> 'array'
               OR pg_catalog.jsonb_array_length(p_request -> 'related_annotation_ids') > 20000
               OR EXISTS (
                    SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request -> 'related_annotation_ids') AS ids(value)
                    WHERE pg_catalog.jsonb_typeof(ids.value) <> 'string'
               )
               OR pg_catalog.jsonb_typeof(p_request -> 'source_versions') <> 'object'
               OR pg_catalog.jsonb_object_length(p_request -> 'source_versions') > 20000
               OR EXISTS (
                    SELECT 1 FROM pg_catalog.jsonb_each(p_request -> 'source_versions') AS versions(key,value)
                    WHERE pg_catalog.jsonb_typeof(versions.value) <> 'number'
                       OR (versions.value #>> '{}')::numeric < 1
                       OR (versions.value #>> '{}')::numeric <> pg_catalog.trunc((versions.value #>> '{}')::numeric)
               )
               OR pg_catalog.jsonb_typeof(p_request -> 'code') <> 'string'
               OR pg_catalog.char_length(p_request ->> 'code') NOT BETWEEN 1 AND 64
               OR p_request ->> 'code' NOT IN ('low_point_count','size_outlier','ground_clearance','temporal_jump','track_gap','track_identity_drift','duplicate_track_member','projection_residual')
               OR pg_catalog.jsonb_typeof(p_request -> 'rule_version') <> 'number'
               OR (p_request ->> 'rule_version')::numeric < 1
               OR (p_request ->> 'rule_version')::numeric <> pg_catalog.trunc((p_request ->> 'rule_version')::numeric)
               OR pg_catalog.jsonb_typeof(p_request -> 'severity') <> 'string'
               OR p_request ->> 'severity' NOT IN ('blocker','warning','info')
               OR pg_catalog.jsonb_typeof(p_request -> 'severity_rank') <> 'number'
               OR (p_request ->> 'severity_rank')::numeric NOT BETWEEN 0 AND 2
               OR (p_request ->> 'severity_rank')::numeric <> pg_catalog.trunc((p_request ->> 'severity_rank')::numeric)
               OR NOT ((p_request ->> 'severity' = 'blocker' AND (p_request ->> 'severity_rank')::integer = 0)
                       OR (p_request ->> 'severity' = 'warning' AND (p_request ->> 'severity_rank')::integer = 1)
                       OR (p_request ->> 'severity' = 'info' AND (p_request ->> 'severity_rank')::integer = 2))
               OR (p_request ? 'annotation_id' AND pg_catalog.jsonb_typeof(p_request -> 'annotation_id') NOT IN ('string','null'))
               OR (p_request ? 'annotation_version' AND pg_catalog.jsonb_typeof(p_request -> 'annotation_version') NOT IN ('number','null'))
               OR (pg_catalog.jsonb_typeof(p_request -> 'annotation_version') = 'number' AND ((p_request ->> 'annotation_version')::numeric < 1 OR (p_request ->> 'annotation_version')::numeric <> pg_catalog.trunc((p_request ->> 'annotation_version')::numeric)))
               OR (p_request ? 'scene_track_id' AND pg_catalog.jsonb_typeof(p_request -> 'scene_track_id') NOT IN ('string','null'))
               OR (p_request ? 'track_revision' AND pg_catalog.jsonb_typeof(p_request -> 'track_revision') NOT IN ('number','null'))
               OR (pg_catalog.jsonb_typeof(p_request -> 'track_revision') = 'number' AND ((p_request ->> 'track_revision')::numeric < 1 OR (p_request ->> 'track_revision')::numeric <> pg_catalog.trunc((p_request ->> 'track_revision')::numeric)))
               OR (p_request ? 'frame_start' AND pg_catalog.jsonb_typeof(p_request -> 'frame_start') NOT IN ('number','null'))
               OR (p_request ? 'frame_end' AND pg_catalog.jsonb_typeof(p_request -> 'frame_end') NOT IN ('number','null'))
               OR (p_request ? 'class_name' AND pg_catalog.jsonb_typeof(p_request -> 'class_name') NOT IN ('string','null'))
               OR (p_request ? 'class_name' AND pg_catalog.jsonb_typeof(p_request -> 'class_name') = 'string' AND pg_catalog.char_length(p_request ->> 'class_name') > 100)
               OR (p_request ? 'task_id' AND pg_catalog.jsonb_typeof(p_request -> 'task_id') NOT IN ('string','null'))
               OR (p_request ? 'frame_start' AND pg_catalog.jsonb_typeof(p_request -> 'frame_start') = 'number' AND (p_request ->> 'frame_start')::numeric <> pg_catalog.trunc((p_request ->> 'frame_start')::numeric))
               OR (p_request ? 'frame_end' AND pg_catalog.jsonb_typeof(p_request -> 'frame_end') = 'number' AND (p_request ->> 'frame_end')::numeric <> pg_catalog.trunc((p_request ->> 'frame_end')::numeric))
               OR ((p_request ? 'frame_start') <> (p_request ? 'frame_end'))
               OR (p_request ? 'frame_start' AND p_request ? 'frame_end' AND ((pg_catalog.jsonb_typeof(p_request -> 'frame_start') = 'null') <> (pg_catalog.jsonb_typeof(p_request -> 'frame_end') = 'null')))
               OR (p_request ? 'frame_start' AND pg_catalog.jsonb_typeof(p_request -> 'frame_start') = 'number' AND (p_request ->> 'frame_start')::numeric < 0)
               OR (p_request ? 'frame_end' AND pg_catalog.jsonb_typeof(p_request -> 'frame_end') = 'number' AND (p_request ->> 'frame_end')::numeric < 0)
               OR (p_request ? 'frame_start' AND pg_catalog.jsonb_typeof(p_request -> 'frame_start') = 'number' AND (p_request ->> 'frame_end')::integer < (p_request ->> 'frame_start')::integer)
               OR (p_request ->> 'dedupe_key' !~ '^[0-9a-f]{64}$')
               OR pg_catalog.jsonb_typeof(p_request -> 'metric') <> 'object'
               OR pg_catalog.jsonb_typeof(p_request -> 'threshold') <> 'object'
               OR pg_catalog.jsonb_typeof(p_request -> 'evidence') <> 'object'
               OR pg_catalog.jsonb_typeof(p_request -> 'locator') <> 'object'
               OR pg_catalog.jsonb_typeof(p_request -> 'dedupe_key') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            IF p_request ->> 'dedupe_key' !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.point_cloud_quality_issues AS current_issue (
                id, run_id, last_seen_run_id, project_id, scene_id, task_id,
                annotation_id, annotation_version, scene_track_id, track_revision,
                related_annotation_ids, source_versions, class_name, code, rule_version,
                severity, severity_rank, status, frame_start, frame_end, metric, threshold,
                evidence, locator, suggested_command, dedupe_key
            ) VALUES (
                (p_request ->> 'new_issue_id')::uuid,
                (p_request ->> 'run_id')::uuid,
                (p_request ->> 'run_id')::uuid,
                (p_request ->> 'project_id')::uuid,
                (p_request ->> 'scene_id')::uuid,
                NULLIF(p_request ->> 'task_id','')::uuid,
                NULLIF(p_request ->> 'annotation_id','')::uuid,
                NULLIF(p_request ->> 'annotation_version','')::integer,
                NULLIF(p_request ->> 'scene_track_id','')::uuid,
                NULLIF(p_request ->> 'track_revision','')::integer,
                ARRAY(SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'related_annotation_ids') AS values(value)),
                p_request -> 'source_versions',
                p_request ->> 'class_name', p_request ->> 'code', (p_request ->> 'rule_version')::integer,
                p_request ->> 'severity', (p_request ->> 'severity_rank')::smallint, 'open',
                NULLIF(p_request ->> 'frame_start','')::integer,
                NULLIF(p_request ->> 'frame_end','')::integer,
                p_request -> 'metric', p_request -> 'threshold', p_request -> 'evidence',
                p_request -> 'locator', p_request ->> 'suggested_command', p_request ->> 'dedupe_key'
            )
            ON CONFLICT (project_id,dedupe_key) DO UPDATE
            SET run_id = EXCLUDED.run_id,
                last_seen_run_id = EXCLUDED.last_seen_run_id,
                task_id = EXCLUDED.task_id,
                annotation_id = EXCLUDED.annotation_id,
                annotation_version = EXCLUDED.annotation_version,
                scene_track_id = EXCLUDED.scene_track_id,
                track_revision = EXCLUDED.track_revision,
                related_annotation_ids = EXCLUDED.related_annotation_ids,
                source_versions = EXCLUDED.source_versions,
                class_name = EXCLUDED.class_name,
                severity = EXCLUDED.severity,
                severity_rank = EXCLUDED.severity_rank,
                metric = EXCLUDED.metric,
                threshold = EXCLUDED.threshold,
                evidence = EXCLUDED.evidence,
                locator = EXCLUDED.locator,
                suggested_command = EXCLUDED.suggested_command,
                status = CASE WHEN current_issue.status = 'stale' THEN 'open' ELSE current_issue.status END,
                resolution_reason = CASE WHEN current_issue.status = 'stale' THEN NULL ELSE current_issue.resolution_reason END,
                resolved_by_id = CASE WHEN current_issue.status = 'stale' THEN NULL ELSE current_issue.resolved_by_id END,
                resolved_at = CASE WHEN current_issue.status = 'stale' THEN NULL ELSE current_issue.resolved_at END,
                review_verdict = CASE WHEN current_issue.status = 'stale' THEN NULL ELSE current_issue.review_verdict END,
                review_note = CASE WHEN current_issue.status = 'stale' THEN NULL ELSE current_issue.review_note END,
                reviewed_by_id = CASE WHEN current_issue.status = 'stale' THEN NULL ELSE current_issue.reviewed_by_id END,
                reviewed_at = CASE WHEN current_issue.status = 'stale' THEN NULL ELSE current_issue.reviewed_at END,
                updated_at = pg_catalog.transaction_timestamp()
            RETURNING * INTO v_issue;
            v_data := pg_catalog.jsonb_build_object('row', pg_catalog.to_jsonb(v_issue));
        WHEN 'get_point_issue', 'lock_point_issue' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'issue_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            IF v_action = 'lock_point_issue' THEN
                SELECT row.* INTO v_issue FROM public.point_cloud_quality_issues AS row WHERE row.id = (p_request ->> 'issue_id')::uuid FOR UPDATE NOWAIT;
            ELSE
                SELECT row.* INTO v_issue FROM public.point_cloud_quality_issues AS row WHERE row.id = (p_request ->> 'issue_id')::uuid;
            END IF;
            v_data := pg_catalog.jsonb_build_object('row', CASE WHEN v_issue.id IS NULL THEN NULL ELSE pg_catalog.to_jsonb(v_issue) END);
        WHEN 'list_point_issues' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'limit') <> 'number'
               OR pg_catalog.jsonb_typeof(p_request -> 'offset') <> 'number'
               OR (p_request ->> 'limit')::numeric <> pg_catalog.trunc((p_request ->> 'limit')::numeric)
               OR (p_request ->> 'offset')::numeric <> pg_catalog.trunc((p_request ->> 'offset')::numeric)
               OR (p_request ->> 'limit')::integer NOT BETWEEN 1 AND 5000
               OR (p_request ->> 'offset')::integer NOT BETWEEN 0 AND 100000
               OR (p_request ? 'code_filter' AND pg_catalog.jsonb_typeof(p_request -> 'code_filter') NOT IN ('string','null'))
               OR (p_request ? 'scene_filter' AND pg_catalog.jsonb_typeof(p_request -> 'scene_filter') NOT IN ('string','null'))
               OR (p_request ? 'task_filter' AND pg_catalog.jsonb_typeof(p_request -> 'task_filter') NOT IN ('string','null'))
               OR (p_request ? 'annotation_filter' AND pg_catalog.jsonb_typeof(p_request -> 'annotation_filter') NOT IN ('string','null'))
               OR (p_request ? 'track_filter' AND pg_catalog.jsonb_typeof(p_request -> 'track_filter') NOT IN ('string','null'))
               OR (p_request ? 'review_status' AND pg_catalog.jsonb_typeof(p_request -> 'review_status') NOT IN ('string','null'))
               OR (p_request ->> 'review_status' IS NOT NULL AND p_request ->> 'review_status' NOT IN ('open','resolved','wont_fix','stale'))
               OR (p_request ? 'severity_filter' AND pg_catalog.jsonb_typeof(p_request -> 'severity_filter') NOT IN ('string','null'))
               OR (p_request ->> 'severity_filter' IS NOT NULL AND p_request ->> 'severity_filter' NOT IN ('blocker','warning','info'))
               OR (p_request ? 'frame_filter' AND pg_catalog.jsonb_typeof(p_request -> 'frame_filter') NOT IN ('number','null'))
               OR (pg_catalog.jsonb_typeof(p_request -> 'frame_filter') = 'number' AND ((p_request ->> 'frame_filter')::numeric < 0 OR (p_request ->> 'frame_filter')::numeric <> pg_catalog.trunc((p_request ->> 'frame_filter')::numeric)))
               OR (p_request ? 'allowed_task_ids' AND pg_catalog.jsonb_typeof(p_request -> 'allowed_task_ids') <> 'array')
               OR (p_request ? 'allowed_task_ids' AND pg_catalog.jsonb_typeof(p_request -> 'allowed_task_ids') = 'array' AND pg_catalog.jsonb_array_length(p_request -> 'allowed_task_ids') > 5000)
               OR (pg_catalog.jsonb_typeof(p_request -> 'code_filter') = 'string' AND pg_catalog.char_length(p_request ->> 'code_filter') > 64) THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_filter_status := NULLIF(p_request ->> 'review_status','');
            v_limit := (p_request ->> 'limit')::integer;
            v_offset := (p_request ->> 'offset')::integer;
            IF v_limit NOT BETWEEN 1 AND 5000 OR v_offset NOT BETWEEN 0 AND 100000 THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            WITH filtered AS (
                SELECT issue_row.*
                FROM public.point_cloud_quality_issues AS issue_row
                WHERE issue_row.project_id = (p_request ->> 'project_id')::uuid
                  AND CASE
                       WHEN NOT (p_request ? 'allowed_task_ids') THEN true
                       ELSE issue_row.task_id = ANY(ARRAY(SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'allowed_task_ids') AS values(value)))
                      END
                  AND (NULLIF(p_request ->> 'severity_filter','') IS NULL OR issue_row.severity = p_request ->> 'severity_filter')
                  AND (NULLIF(p_request ->> 'code_filter','') IS NULL OR issue_row.code = p_request ->> 'code_filter')
                  AND (NULLIF(p_request ->> 'scene_filter','') IS NULL OR issue_row.scene_id = (p_request ->> 'scene_filter')::uuid)
                  AND (NULLIF(p_request ->> 'task_filter','') IS NULL OR issue_row.task_id = (p_request ->> 'task_filter')::uuid)
                  AND (NULLIF(p_request ->> 'annotation_filter','') IS NULL OR issue_row.annotation_id = (p_request ->> 'annotation_filter')::uuid OR issue_row.related_annotation_ids @> ARRAY[(p_request ->> 'annotation_filter')::uuid])
                  AND (NULLIF(p_request ->> 'track_filter','') IS NULL OR issue_row.scene_track_id = (p_request ->> 'track_filter')::uuid)
                  AND (NULLIF(p_request ->> 'frame_filter','') IS NULL OR (issue_row.frame_start <= (p_request ->> 'frame_filter')::integer AND issue_row.frame_end >= (p_request ->> 'frame_filter')::integer))
                  AND (v_filter_status IS NULL
                       OR (v_filter_status = 'stale' AND issue_row.status <> 'stale')
                       OR (v_filter_status <> 'stale' AND issue_row.status = v_filter_status))
            ),
            page AS (
                SELECT filtered.*
                FROM filtered
                ORDER BY filtered.severity_rank, filtered.created_at DESC, filtered.id
                OFFSET CASE WHEN v_filter_status IS NULL THEN v_offset ELSE 0 END
                LIMIT CASE WHEN v_filter_status IS NULL THEN v_limit ELSE NULL::integer END
            )
            SELECT COALESCE((SELECT pg_catalog.jsonb_agg(pg_catalog.to_jsonb(page) ORDER BY page.severity_rank, page.created_at DESC, page.id) FROM page), '[]'::jsonb),
                   (SELECT pg_catalog.count(*) FROM filtered)
            INTO v_rows, v_total;
            v_data := pg_catalog.jsonb_build_object('rows',v_rows,'total',v_total);
        WHEN 'mark_point_issues_stale' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'stale_issue_ids') <> 'array'
               OR pg_catalog.jsonb_array_length(p_request -> 'stale_issue_ids') > 20000 THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            WITH changed AS (
                UPDATE public.point_cloud_quality_issues AS row
                SET status = 'stale', resolved_at = NULL, resolved_by_id = NULL,
                    updated_at = pg_catalog.transaction_timestamp()
                WHERE row.id = ANY(ARRAY(SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'stale_issue_ids') AS values(value)))
                  AND row.status <> 'stale'
                RETURNING row.id
            ) SELECT pg_catalog.count(*) INTO v_total FROM changed;
            v_data := pg_catalog.jsonb_build_object('changed',v_total);
        WHEN 'decide_point_issue' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'issue_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'actor_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'review_status') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'resolved_at') NOT IN ('string','null')
               OR pg_catalog.jsonb_typeof(p_request -> 'expected_task_id') NOT IN ('string','null')
               OR (p_request ? 'resolution_reason' AND pg_catalog.jsonb_typeof(p_request -> 'resolution_reason') NOT IN ('string','null'))
               OR (p_request ? 'review_verdict' AND pg_catalog.jsonb_typeof(p_request -> 'review_verdict') NOT IN ('string','null'))
               OR (p_request ? 'review_note' AND pg_catalog.jsonb_typeof(p_request -> 'review_note') NOT IN ('string','null'))
               OR (p_request ? 'reviewed_at' AND pg_catalog.jsonb_typeof(p_request -> 'reviewed_at') NOT IN ('string','null'))
               OR pg_catalog.char_length(COALESCE(p_request ->> 'resolution_reason','')) > 2000
               OR pg_catalog.char_length(COALESCE(p_request ->> 'review_note','')) > 2000 THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            IF p_request ->> 'review_status' NOT IN ('open','resolved','wont_fix') THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            IF (p_request ->> 'review_status' = 'open' AND p_request ->> 'review_verdict' IS NOT NULL)
               OR (p_request ->> 'review_status' <> 'open' AND pg_catalog.jsonb_typeof(p_request -> 'resolved_at') <> 'string')
               OR (p_request ->> 'review_verdict' IS NOT NULL AND pg_catalog.jsonb_typeof(p_request -> 'reviewed_at') <> 'string')
               OR (p_request ->> 'review_verdict' IS NULL AND p_request ? 'reviewed_at' AND pg_catalog.jsonb_typeof(p_request -> 'reviewed_at') = 'string')
               OR (p_request ->> 'review_verdict' IS NOT NULL AND p_request ->> 'review_verdict' NOT IN ('confirmed','false_positive','accepted_exception','uncertain'))
               OR (p_request ->> 'review_verdict' = 'confirmed' AND p_request ->> 'review_status' <> 'resolved')
               OR (p_request ->> 'review_verdict' IN ('false_positive','accepted_exception','uncertain') AND p_request ->> 'review_status' <> 'wont_fix') THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            UPDATE public.point_cloud_quality_issues AS row
            SET status = p_request ->> 'review_status',
                resolution_reason = NULLIF(pg_catalog.btrim(p_request ->> 'resolution_reason'),''),
                resolved_by_id = CASE WHEN p_request ->> 'review_status' = 'open' THEN NULL ELSE (p_request ->> 'actor_id')::uuid END,
                resolved_at = CASE WHEN p_request ->> 'review_status' = 'open' THEN NULL ELSE (p_request ->> 'resolved_at')::timestamptz END,
                review_verdict = CASE WHEN p_request ->> 'review_status' = 'open' THEN NULL ELSE p_request ->> 'review_verdict' END,
                review_note = CASE WHEN p_request ->> 'review_status' = 'open' THEN NULL ELSE NULLIF(pg_catalog.btrim(p_request ->> 'review_note'),'') END,
                reviewed_by_id = CASE WHEN p_request ->> 'review_status' = 'open' OR p_request ->> 'review_verdict' IS NULL THEN NULL ELSE (p_request ->> 'actor_id')::uuid END,
                reviewed_at = CASE WHEN p_request ->> 'review_status' = 'open' OR p_request ->> 'review_verdict' IS NULL THEN NULL ELSE NULLIF(p_request ->> 'reviewed_at','')::timestamptz END,
                updated_at = pg_catalog.transaction_timestamp()
            WHERE row.id = (p_request ->> 'issue_id')::uuid
              AND row.task_id IS NOT DISTINCT FROM NULLIF(p_request ->> 'expected_task_id','')::uuid
            RETURNING * INTO v_issue;
            v_data := pg_catalog.jsonb_build_object(
                'row',CASE WHEN v_issue.id IS NULL THEN NULL ELSE pg_catalog.jsonb_build_object(
                    'id',v_issue.id,'status',v_issue.status
                ) END
            );
        WHEN 'list_point_evaluation_samples' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'cutoff_at') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'limit') <> 'number'
               OR (p_request ->> 'limit')::numeric <> pg_catalog.trunc((p_request ->> 'limit')::numeric)
               OR (p_request ->> 'limit')::integer NOT BETWEEN 1 AND 20001 THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(issue_row) ORDER BY issue_row.id),'[]'::jsonb)
            INTO v_rows
            FROM (
                SELECT row.*
                FROM public.point_cloud_quality_issues AS row
                WHERE row.project_id = (p_request ->> 'project_id')::uuid
                  AND row.review_verdict IS NOT NULL
                  AND row.status <> 'stale'
                  AND row.reviewed_at <= (p_request ->> 'cutoff_at')::timestamptz
                ORDER BY row.id
                LIMIT (p_request ->> 'limit')::integer
            ) AS issue_row;
            v_data := pg_catalog.jsonb_build_object('rows',COALESCE(v_rows,'[]'::jsonb));
        WHEN 'create_point_evaluation' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'new_evaluation_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'actor_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'baseline_config_revision') <> 'number'
               OR (p_request ->> 'baseline_config_revision')::numeric <> pg_catalog.trunc((p_request ->> 'baseline_config_revision')::numeric)
               OR (p_request ->> 'baseline_config_revision')::integer < 1
               OR pg_catalog.jsonb_typeof(p_request -> 'baseline_config_digest') <> 'string'
               OR p_request ->> 'baseline_config_digest' !~ '^[0-9a-f]{64}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'baseline_config_snapshot') <> 'object'
               OR pg_catalog.jsonb_typeof(p_request -> 'candidate_config_digest') <> 'string'
               OR p_request ->> 'candidate_config_digest' !~ '^[0-9a-f]{64}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'candidate_config_snapshot') <> 'object'
               OR pg_catalog.jsonb_typeof(p_request -> 'cutoff_at') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'sample_count') <> 'number'
               OR (p_request ->> 'sample_count')::numeric <> pg_catalog.trunc((p_request ->> 'sample_count')::numeric)
               OR (p_request ->> 'sample_count')::integer < 0
               OR pg_catalog.jsonb_typeof(p_request -> 'sample_snapshot') <> 'array'
               OR pg_catalog.jsonb_typeof(p_request -> 'summary') <> 'object'
               OR pg_catalog.jsonb_typeof(p_request -> 'gate_status') <> 'string'
               OR p_request ->> 'gate_status' NOT IN ('insufficient_data','hold','promote')
               OR pg_catalog.jsonb_array_length(p_request -> 'sample_snapshot') <> (p_request ->> 'sample_count')::integer
               OR pg_catalog.jsonb_typeof(p_request -> 'gate_reasons') <> 'array' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.point_cloud_quality_evaluations (
                id, project_id, created_by_id, baseline_config_revision,
                baseline_config_digest, baseline_config_snapshot, candidate_config_digest,
                candidate_config_snapshot, cutoff_at, sample_count, sample_snapshot,
                summary, gate_status, gate_reasons
            ) VALUES (
                (p_request ->> 'new_evaluation_id')::uuid,
                (p_request ->> 'project_id')::uuid,
                (p_request ->> 'actor_id')::uuid,
                (p_request ->> 'baseline_config_revision')::integer,
                p_request ->> 'baseline_config_digest', p_request -> 'baseline_config_snapshot',
                p_request ->> 'candidate_config_digest', p_request -> 'candidate_config_snapshot',
                (p_request ->> 'cutoff_at')::timestamptz,
                (p_request ->> 'sample_count')::integer, p_request -> 'sample_snapshot',
                p_request -> 'summary', p_request ->> 'gate_status', p_request -> 'gate_reasons'
            ) RETURNING * INTO v_evaluation;
            v_data := pg_catalog.jsonb_build_object('row',pg_catalog.to_jsonb(v_evaluation));
        WHEN 'get_point_evaluation' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'evaluation_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            SELECT row.* INTO v_evaluation FROM public.point_cloud_quality_evaluations AS row WHERE row.id = (p_request ->> 'evaluation_id')::uuid;
            v_data := pg_catalog.jsonb_build_object('row',CASE WHEN v_evaluation.id IS NULL THEN NULL ELSE pg_catalog.to_jsonb(v_evaluation) END);
        WHEN 'list_point_evaluations' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'limit') <> 'number'
               OR pg_catalog.jsonb_typeof(p_request -> 'offset') <> 'number'
               OR (p_request ->> 'limit')::numeric <> pg_catalog.trunc((p_request ->> 'limit')::numeric)
               OR (p_request ->> 'offset')::numeric <> pg_catalog.trunc((p_request ->> 'offset')::numeric)
               OR (p_request ->> 'limit')::integer NOT BETWEEN 1 AND 100
               OR (p_request ->> 'offset')::integer NOT BETWEEN 0 AND 100000 THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_limit := (p_request ->> 'limit')::integer;
            v_offset := (p_request ->> 'offset')::integer;
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(page_row) ORDER BY page_row.created_at DESC,page_row.id),'[]'::jsonb),
                   (SELECT pg_catalog.count(*) FROM public.point_cloud_quality_evaluations AS row WHERE row.project_id = (p_request ->> 'project_id')::uuid)
            INTO v_rows,v_total
            FROM (
                SELECT row.* FROM public.point_cloud_quality_evaluations AS row
                WHERE row.project_id = (p_request ->> 'project_id')::uuid
                ORDER BY row.created_at DESC,row.id
                OFFSET v_offset LIMIT v_limit
            ) AS page_row;
            v_data := pg_catalog.jsonb_build_object('rows',COALESCE(v_rows,'[]'::jsonb),'total',v_total);
        WHEN 'promote_point_evaluation' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'evaluation_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'actor_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'promoted_config_revision') <> 'number'
               OR pg_catalog.jsonb_typeof(p_request -> 'promoted_at') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            UPDATE public.point_cloud_quality_evaluations AS row
            SET promoted_by_id = (p_request ->> 'actor_id')::uuid,
                promoted_at = (p_request ->> 'promoted_at')::timestamptz,
                promoted_config_revision = (p_request ->> 'promoted_config_revision')::integer
            WHERE row.id = (p_request ->> 'evaluation_id')::uuid
            RETURNING * INTO v_evaluation;
            v_data := pg_catalog.jsonb_build_object('row',CASE WHEN v_evaluation.id IS NULL THEN NULL ELSE pg_catalog.to_jsonb(v_evaluation) END);
        WHEN 'find_video_run' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'task_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'left_segment_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'right_segment_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'input_digest') <> 'string'
               OR p_request ->> 'input_digest' !~ '^[0-9a-f]{64}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'statuses') <> 'array'
               OR pg_catalog.jsonb_array_length(p_request -> 'statuses') > 8
               OR EXISTS (
                    SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request -> 'statuses') AS values(value)
                    WHERE pg_catalog.jsonb_typeof(values.value) <> 'string'
                       OR values.value #>> '{}' NOT IN ('pending','running','completed','empty_overlap','accepted','failed','stale')
               ) THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            SELECT row.* INTO v_video_run
            FROM public.video_track_quality_runs AS row
            WHERE row.task_id = (p_request ->> 'task_id')::uuid
              AND row.left_segment_id = (p_request ->> 'left_segment_id')::uuid
              AND row.right_segment_id = (p_request ->> 'right_segment_id')::uuid
              AND row.input_digest = p_request ->> 'input_digest'
              AND row.status = ANY(ARRAY(SELECT value FROM pg_catalog.jsonb_array_elements_text(p_request -> 'statuses') AS values(value)))
            ORDER BY row.created_at DESC LIMIT 1;
            v_data := pg_catalog.jsonb_build_object(
                'row',CASE WHEN v_video_run.id IS NULL THEN NULL ELSE pg_catalog.jsonb_build_object(
                    'id',v_video_run.id,'status',v_video_run.status
                ) END
            );
        WHEN 'create_video_run' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'new_run_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'task_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'left_segment_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'right_segment_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'async_job_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'actor_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'input_snapshot') <> 'array'
               OR pg_catalog.jsonb_typeof(p_request -> 'input_digest') <> 'string'
               OR p_request ->> 'input_digest' !~ '^[0-9a-f]{64}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'sampling_digest') <> 'string'
               OR p_request ->> 'sampling_digest' !~ '^[0-9a-f]{64}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'metrics') <> 'object' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.video_track_quality_runs (
                id,task_id,left_segment_id,right_segment_id,async_job_id,requested_by_id,
                status,progress_pct,input_snapshot,input_digest,sampling_digest,metrics,pairs
            ) VALUES (
                (p_request ->> 'new_run_id')::uuid,
                (p_request ->> 'task_id')::uuid,
                (p_request ->> 'left_segment_id')::uuid,
                (p_request ->> 'right_segment_id')::uuid,
                (p_request ->> 'async_job_id')::uuid,
                (p_request ->> 'actor_id')::uuid,
                'pending',0,p_request -> 'input_snapshot',p_request ->> 'input_digest',
                p_request ->> 'sampling_digest',p_request -> 'metrics','[]'::jsonb
            ) RETURNING * INTO v_video_run;
            v_data := pg_catalog.jsonb_build_object('row',pg_catalog.to_jsonb(v_video_run));
        WHEN 'get_video_run', 'lock_video_run' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'run_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            IF v_action = 'lock_video_run' THEN
                SELECT row.* INTO v_video_run FROM public.video_track_quality_runs AS row WHERE row.id = (p_request ->> 'run_id')::uuid FOR UPDATE;
            ELSE
                SELECT row.* INTO v_video_run FROM public.video_track_quality_runs AS row WHERE row.id = (p_request ->> 'run_id')::uuid;
            END IF;
            v_data := pg_catalog.jsonb_build_object('row',CASE WHEN v_video_run.id IS NULL THEN NULL ELSE pg_catalog.to_jsonb(v_video_run) END);
        WHEN 'list_video_runs' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'task_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(row) ORDER BY row.created_at DESC),'[]'::jsonb)
            INTO v_rows FROM public.video_track_quality_runs AS row
            WHERE row.task_id = (p_request ->> 'task_id')::uuid;
            v_data := pg_catalog.jsonb_build_object('rows',COALESCE(v_rows,'[]'::jsonb));
        WHEN 'list_accepted_video_runs' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'task_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'run_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(row) ORDER BY row.created_at DESC),'[]'::jsonb)
            INTO v_rows FROM public.video_track_quality_runs AS row
            WHERE row.task_id = (p_request ->> 'task_id')::uuid
              AND row.status = 'accepted'
              AND row.id <> (p_request ->> 'run_id')::uuid;
            v_data := pg_catalog.jsonb_build_object('rows',COALESCE(v_rows,'[]'::jsonb));
        WHEN 'update_video_run' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'run_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'status') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_status := p_request ->> 'status';
            IF v_status = 'running' THEN
                IF pg_catalog.jsonb_object_length(p_request) <> 6 OR NOT (p_request ?& ARRAY['started_at','progress_pct']) OR pg_catalog.jsonb_typeof(p_request -> 'started_at') <> 'string' OR pg_catalog.jsonb_typeof(p_request -> 'progress_pct') <> 'number' OR (p_request ->> 'progress_pct')::numeric NOT BETWEEN 0 AND 100 OR (p_request ->> 'progress_pct')::numeric <> pg_catalog.trunc((p_request ->> 'progress_pct')::numeric) THEN
                    RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
                END IF;
                UPDATE public.video_track_quality_runs AS row
                SET status='running',started_at=(p_request ->> 'started_at')::timestamptz,
                    progress_pct=(p_request ->> 'progress_pct')::integer,updated_at=pg_catalog.transaction_timestamp()
                WHERE row.id=(p_request ->> 'run_id')::uuid RETURNING * INTO v_video_run;
            ELSIF v_status IN ('completed','empty_overlap') THEN
                IF pg_catalog.jsonb_object_length(p_request) <> 8 OR NOT (p_request ?& ARRAY['metrics','pairs','progress_pct','completed_at']) OR pg_catalog.jsonb_typeof(p_request -> 'metrics') <> 'object' OR pg_catalog.jsonb_typeof(p_request -> 'pairs') <> 'array' OR pg_catalog.jsonb_typeof(p_request -> 'progress_pct') <> 'number' OR (p_request ->> 'progress_pct')::numeric NOT BETWEEN 0 AND 100 OR (p_request ->> 'progress_pct')::numeric <> pg_catalog.trunc((p_request ->> 'progress_pct')::numeric) OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') <> 'string' THEN
                    RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
                END IF;
                UPDATE public.video_track_quality_runs AS row
                SET status=v_status,metrics=p_request -> 'metrics',pairs=p_request -> 'pairs',
                    progress_pct=(p_request ->> 'progress_pct')::integer,
                    completed_at=(p_request ->> 'completed_at')::timestamptz,
                    updated_at=pg_catalog.transaction_timestamp()
                WHERE row.id=(p_request ->> 'run_id')::uuid RETURNING * INTO v_video_run;
            ELSIF v_status = 'accepted' THEN
                IF pg_catalog.jsonb_object_length(p_request) <> 7 OR NOT (p_request ?& ARRAY['accepted_by_id','accepted_at','pairs']) OR pg_catalog.jsonb_typeof(p_request -> 'accepted_by_id') <> 'string' OR pg_catalog.jsonb_typeof(p_request -> 'accepted_at') <> 'string' OR pg_catalog.jsonb_typeof(p_request -> 'pairs') <> 'array' THEN
                    RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
                END IF;
                UPDATE public.video_track_quality_runs AS row
                SET status='accepted',accepted_by_id=(p_request ->> 'accepted_by_id')::uuid,
                    accepted_at=(p_request ->> 'accepted_at')::timestamptz,pairs=p_request -> 'pairs',
                    updated_at=pg_catalog.transaction_timestamp()
                WHERE row.id=(p_request ->> 'run_id')::uuid RETURNING * INTO v_video_run;
            ELSIF v_status = 'stale' THEN
                IF NOT (p_request ? 'stale_at') OR pg_catalog.jsonb_typeof(p_request -> 'stale_at') <> 'string'
                   OR (p_request ? 'completed_at' AND pg_catalog.jsonb_typeof(p_request -> 'completed_at') <> 'string')
                   OR (p_request ? 'error_message' AND pg_catalog.jsonb_typeof(p_request -> 'error_message') <> 'string')
                   OR pg_catalog.jsonb_object_length(p_request) NOT IN (5,6,7) THEN
                    RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
                END IF;
                UPDATE public.video_track_quality_runs AS row
                SET status='stale',stale_at=(p_request ->> 'stale_at')::timestamptz,
                    completed_at=CASE WHEN p_request ? 'completed_at' THEN (p_request ->> 'completed_at')::timestamptz ELSE row.completed_at END,
                    error_message=CASE WHEN p_request ? 'error_message' THEN left(p_request ->> 'error_message',4000) ELSE row.error_message END,
                    updated_at=pg_catalog.transaction_timestamp()
                WHERE row.id=(p_request ->> 'run_id')::uuid RETURNING * INTO v_video_run;
            ELSIF v_status = 'failed' THEN
                IF pg_catalog.jsonb_object_length(p_request) <> 6 OR NOT (p_request ?& ARRAY['error_message','completed_at']) OR pg_catalog.jsonb_typeof(p_request -> 'error_message') <> 'string' OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') <> 'string' THEN
                    RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
                END IF;
                UPDATE public.video_track_quality_runs AS row
                SET status='failed',error_message=left(p_request ->> 'error_message',4000),
                    completed_at=(p_request ->> 'completed_at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                WHERE row.id=(p_request ->> 'run_id')::uuid RETURNING * INTO v_video_run;
            ELSE
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_data := pg_catalog.jsonb_build_object(
                'row',CASE WHEN v_video_run.id IS NULL THEN NULL ELSE pg_catalog.jsonb_build_object(
                    'id',v_video_run.id,'status',v_video_run.status
                ) END
            );
        WHEN 'replace_video_issues' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'run_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'issue_rows') <> 'array' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            DELETE FROM public.video_track_quality_issues AS row WHERE row.run_id = (p_request ->> 'run_id')::uuid;
            FOR v_item IN SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'issue_rows') AS input_rows(value)
            LOOP
                IF pg_catalog.jsonb_typeof(v_item) <> 'object'
                   OR pg_catalog.jsonb_object_length(v_item) <> 7
                   OR NOT (v_item ?& ARRAY['id','left_annotation_id','right_annotation_id','code','frame_start','frame_end','metric'])
                   OR pg_catalog.jsonb_typeof(v_item -> 'id') <> 'string'
                   OR pg_catalog.jsonb_typeof(v_item -> 'code') <> 'string'
                   OR pg_catalog.jsonb_typeof(v_item -> 'frame_start') <> 'number'
                   OR pg_catalog.jsonb_typeof(v_item -> 'frame_end') <> 'number'
                   OR pg_catalog.jsonb_typeof(v_item -> 'metric') <> 'object'
                   OR v_item ->> 'code' NOT IN ('false_positive','false_negative','id_switch','fragmentation','geometry_mismatch','unsupported_geometry')
                   OR (v_item ->> 'frame_start')::numeric < 0
                   OR (v_item ->> 'frame_end')::numeric < (v_item ->> 'frame_start')::numeric
                   OR (v_item ->> 'frame_start')::numeric <> pg_catalog.trunc((v_item ->> 'frame_start')::numeric)
                   OR (v_item ->> 'frame_end')::numeric <> pg_catalog.trunc((v_item ->> 'frame_end')::numeric)
                   OR (v_item -> 'left_annotation_id' IS NOT NULL AND pg_catalog.jsonb_typeof(v_item -> 'left_annotation_id') NOT IN ('string','null'))
                   OR (v_item -> 'right_annotation_id' IS NOT NULL AND pg_catalog.jsonb_typeof(v_item -> 'right_annotation_id') NOT IN ('string','null')) THEN
                    RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
                END IF;
                INSERT INTO public.video_track_quality_issues (
                    id,run_id,left_annotation_id,right_annotation_id,code,frame_start,frame_end,metric
                ) VALUES (
                    (v_item ->> 'id')::uuid,
                    (p_request ->> 'run_id')::uuid,
                    NULLIF(v_item ->> 'left_annotation_id','')::uuid,
                    NULLIF(v_item ->> 'right_annotation_id','')::uuid,
                    v_item ->> 'code',(v_item ->> 'frame_start')::integer,
                    (v_item ->> 'frame_end')::integer,v_item -> 'metric'
                );
            END LOOP;
            v_data := pg_catalog.jsonb_build_object('stored',true);
        WHEN 'list_video_issues' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'run_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(row) ORDER BY row.frame_start,row.id),'[]'::jsonb)
            INTO v_rows FROM public.video_track_quality_issues AS row WHERE row.run_id=(p_request ->> 'run_id')::uuid;
            v_data := pg_catalog.jsonb_build_object('rows',COALESCE(v_rows,'[]'::jsonb));
        WHEN 'mark_video_runs_stale' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'segment_ids') <> 'array'
               OR pg_catalog.jsonb_array_length(p_request -> 'segment_ids') > 5000 THEN
                RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
            END IF;
            v_now := pg_catalog.clock_timestamp();
            WITH changed AS (
                UPDATE public.video_track_quality_runs AS row
                SET status='stale',stale_at=v_now,updated_at=pg_catalog.transaction_timestamp()
                WHERE (row.left_segment_id = ANY(ARRAY(SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'segment_ids') AS values(value)))
                       OR row.right_segment_id = ANY(ARRAY(SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'segment_ids') AS values(value)))
                      )
                  AND row.status IN ('completed','empty_overlap','accepted')
                RETURNING row.id
            ) SELECT pg_catalog.count(*) INTO v_total FROM changed;
            v_data := pg_catalog.jsonb_build_object('changed',v_total);
    END CASE;

    RETURN pg_catalog.jsonb_build_object(
        'schema','aap-labeling-spatial-quality/v1',
        'action',v_action,
        'data',v_data
    );
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'invalid Labeling spatial quality request' USING ERRCODE = '22023';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.spatial_quality_command(jsonb) FROM PUBLIC;
