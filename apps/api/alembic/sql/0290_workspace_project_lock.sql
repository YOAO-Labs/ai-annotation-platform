-- Extend the private Project serializer with a blocking exclusive mode. The
-- existing key_share, update, and read branches remain unchanged.
CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1._class_rename_project_row(
    p_project_id uuid,
    p_lock_mode text
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_row_json text;
    v_sqlstate text;
    v_message text;
    v_detail text;
    v_original_hint text;
BEGIN
    IF p_project_id IS NULL OR p_lock_mode IS NULL
       OR p_lock_mode NOT IN ('key_share', 'update', 'read', 'update_wait') THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid class rename Project row request';
    END IF;

    IF p_lock_mode = 'key_share' THEN
        BEGIN
            SELECT aap_workspace_project_query_v1._project_row_json(
                pg_catalog.jsonb_build_object(
                    'id', project.id,
                    'organization_id', project.organization_id,
                    'display_id', project.display_id,
                    'name', project.name,
                    'type_label', project.type_label,
                    'type_key', project.type_key,
                    'data_type', project.data_type,
                    'owner_id', project.owner_id,
                    'status', project.status,
                    'ai_enabled', project.ai_enabled,
                    'ml_backend_pool_id', project.ml_backend_pool_id,
                    'default_variants', project.default_variants::text,
                    'preannotate_pipeline', project.preannotate_pipeline::text,
                    'label_config', project.label_config::text,
                    'tool_bindings', project.tool_bindings::text,
                    'sampling', project.sampling,
                    'maximum_annotations', project.maximum_annotations,
                    'show_overlap_first', project.show_overlap_first,
                    'iou_dedup_threshold', project.iou_dedup_threshold::double precision::text,
                    'box_threshold', project.box_threshold::double precision::text,
                    'text_threshold', project.text_threshold::double precision::text,
                    'ai_interactive_enabled', project.ai_interactive_enabled,
                    'raster_mask_native_editing_enabled', project.raster_mask_native_editing_enabled,
                    'mask_qc_config', project.mask_qc_config::text,
                    'point_cloud_quality_config', project.point_cloud_quality_config::text,
                    'rendering_config', project.rendering_config::text,
                    'annotation_guide', project.annotation_guide,
                    'guide_assets', project.guide_assets::text,
                    'model_version', project.model_version,
                    'task_lock_ttl_seconds', project.task_lock_ttl_seconds,
                    'video_sampling', project.video_sampling::text,
                    'video_collaboration', project.video_collaboration::text,
                    'scene_mode', project.scene_mode,
                    'prefer_same_scene_continuation', project.prefer_same_scene_continuation,
                    'scene_continuation_window_min', project.scene_continuation_window_min,
                    'due_date', pg_catalog.to_json(project.due_date),
                    'created_at', pg_catalog.to_json(project.created_at),
                    'updated_at', pg_catalog.to_json(project.updated_at)
                ),
                counter.total_tasks,
                counter.completed_tasks,
                counter.review_tasks,
                counter.in_progress_tasks,
                counter.batch_summary::text
            )
            INTO v_row_json
            FROM public.projects AS project
            CROSS JOIN LATERAL
                aap_labeling_v1.workspace_project_query_counters(
                    ARRAY[project.id]::uuid[]
                ) AS counter
            WHERE project.id = p_project_id
            FOR KEY SHARE OF project NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            GET STACKED DIAGNOSTICS
                v_sqlstate = RETURNED_SQLSTATE,
                v_message = MESSAGE_TEXT,
                v_detail = PG_EXCEPTION_DETAIL,
                v_original_hint = PG_EXCEPTION_HINT;
            RAISE EXCEPTION USING
                ERRCODE = v_sqlstate,
                MESSAGE = v_message,
                DETAIL = v_detail,
                HINT = 'aap.workspace.configuration.project_busy' || CASE
                    WHEN v_original_hint IS NULL OR v_original_hint = '' THEN ''
                    ELSE E'\n' || v_original_hint
                END;
        END;
    ELSIF p_lock_mode = 'update' THEN
        BEGIN
            SELECT aap_workspace_project_query_v1._project_row_json(
                pg_catalog.jsonb_build_object(
                    'id', project.id,
                    'organization_id', project.organization_id,
                    'display_id', project.display_id,
                    'name', project.name,
                    'type_label', project.type_label,
                    'type_key', project.type_key,
                    'data_type', project.data_type,
                    'owner_id', project.owner_id,
                    'status', project.status,
                    'ai_enabled', project.ai_enabled,
                    'ml_backend_pool_id', project.ml_backend_pool_id,
                    'default_variants', project.default_variants::text,
                    'preannotate_pipeline', project.preannotate_pipeline::text,
                    'label_config', project.label_config::text,
                    'tool_bindings', project.tool_bindings::text,
                    'sampling', project.sampling,
                    'maximum_annotations', project.maximum_annotations,
                    'show_overlap_first', project.show_overlap_first,
                    'iou_dedup_threshold', project.iou_dedup_threshold::double precision::text,
                    'box_threshold', project.box_threshold::double precision::text,
                    'text_threshold', project.text_threshold::double precision::text,
                    'ai_interactive_enabled', project.ai_interactive_enabled,
                    'raster_mask_native_editing_enabled', project.raster_mask_native_editing_enabled,
                    'mask_qc_config', project.mask_qc_config::text,
                    'point_cloud_quality_config', project.point_cloud_quality_config::text,
                    'rendering_config', project.rendering_config::text,
                    'annotation_guide', project.annotation_guide,
                    'guide_assets', project.guide_assets::text,
                    'model_version', project.model_version,
                    'task_lock_ttl_seconds', project.task_lock_ttl_seconds,
                    'video_sampling', project.video_sampling::text,
                    'video_collaboration', project.video_collaboration::text,
                    'scene_mode', project.scene_mode,
                    'prefer_same_scene_continuation', project.prefer_same_scene_continuation,
                    'scene_continuation_window_min', project.scene_continuation_window_min,
                    'due_date', pg_catalog.to_json(project.due_date),
                    'created_at', pg_catalog.to_json(project.created_at),
                    'updated_at', pg_catalog.to_json(project.updated_at)
                ),
                counter.total_tasks,
                counter.completed_tasks,
                counter.review_tasks,
                counter.in_progress_tasks,
                counter.batch_summary::text
            )
            INTO v_row_json
            FROM public.projects AS project
            CROSS JOIN LATERAL
                aap_labeling_v1.workspace_project_query_counters(
                    ARRAY[project.id]::uuid[]
                ) AS counter
            WHERE project.id = p_project_id
            FOR UPDATE OF project NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            GET STACKED DIAGNOSTICS
                v_sqlstate = RETURNED_SQLSTATE,
                v_message = MESSAGE_TEXT,
                v_detail = PG_EXCEPTION_DETAIL,
                v_original_hint = PG_EXCEPTION_HINT;
            RAISE EXCEPTION USING
                ERRCODE = v_sqlstate,
                MESSAGE = v_message,
                DETAIL = v_detail,
                HINT = 'aap.workspace.configuration.project_busy' || CASE
                    WHEN v_original_hint IS NULL OR v_original_hint = '' THEN ''
                    ELSE E'\n' || v_original_hint
                END;
        END;
    ELSIF p_lock_mode = 'update_wait' THEN
        PERFORM project.id
        FROM public.projects AS project
        WHERE project.id = p_project_id
        FOR UPDATE OF project;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        -- Read the typed counters from a fresh statement snapshot after waiting.
        RETURN aap_workspace_configuration_v1._class_rename_project_row(
            p_project_id, 'read'
        );
    ELSE
        SELECT aap_workspace_project_query_v1._project_row_json(
            pg_catalog.jsonb_build_object(
                'id', project.id,
                'organization_id', project.organization_id,
                'display_id', project.display_id,
                'name', project.name,
                'type_label', project.type_label,
                'type_key', project.type_key,
                'data_type', project.data_type,
                'owner_id', project.owner_id,
                'status', project.status,
                'ai_enabled', project.ai_enabled,
                'ml_backend_pool_id', project.ml_backend_pool_id,
                'default_variants', project.default_variants::text,
                'preannotate_pipeline', project.preannotate_pipeline::text,
                'label_config', project.label_config::text,
                'tool_bindings', project.tool_bindings::text,
                'sampling', project.sampling,
                'maximum_annotations', project.maximum_annotations,
                'show_overlap_first', project.show_overlap_first,
                'iou_dedup_threshold', project.iou_dedup_threshold::double precision::text,
                'box_threshold', project.box_threshold::double precision::text,
                'text_threshold', project.text_threshold::double precision::text,
                'ai_interactive_enabled', project.ai_interactive_enabled,
                'raster_mask_native_editing_enabled', project.raster_mask_native_editing_enabled,
                'mask_qc_config', project.mask_qc_config::text,
                'point_cloud_quality_config', project.point_cloud_quality_config::text,
                'rendering_config', project.rendering_config::text,
                'annotation_guide', project.annotation_guide,
                'guide_assets', project.guide_assets::text,
                'model_version', project.model_version,
                'task_lock_ttl_seconds', project.task_lock_ttl_seconds,
                'video_sampling', project.video_sampling::text,
                'video_collaboration', project.video_collaboration::text,
                'scene_mode', project.scene_mode,
                'prefer_same_scene_continuation', project.prefer_same_scene_continuation,
                'scene_continuation_window_min', project.scene_continuation_window_min,
                'due_date', pg_catalog.to_json(project.due_date),
                'created_at', pg_catalog.to_json(project.created_at),
                'updated_at', pg_catalog.to_json(project.updated_at)
            ),
            counter.total_tasks,
            counter.completed_tasks,
            counter.review_tasks,
            counter.in_progress_tasks,
            counter.batch_summary::text
        )
        INTO v_row_json
        FROM public.projects AS project
        CROSS JOIN LATERAL
            aap_labeling_v1.workspace_project_query_counters(
                ARRAY[project.id]::uuid[]
            ) AS counter
        WHERE project.id = p_project_id;
    END IF;

    RETURN v_row_json;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_configuration_v1._class_rename_project_row(uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.lock_project_row(
    p_project_id uuid,
    p_nowait boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_row_json text;
    v_lock_mode text;
BEGIN
    IF p_project_id IS NULL OR p_nowait IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace Project row lock request';
    END IF;

    v_lock_mode := CASE WHEN p_nowait THEN 'update' ELSE 'update_wait' END;
    v_row_json := aap_workspace_configuration_v1._class_rename_project_row(
        p_project_id, v_lock_mode
    );

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-project-lock/v1',
        'project_id', p_project_id,
        'nowait', p_nowait,
        'row_json', CASE
            WHEN v_row_json IS NULL THEN NULL
            ELSE pg_catalog.to_jsonb(v_row_json)
        END
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.lock_project_row(uuid, boolean)
    FROM PUBLIC;
