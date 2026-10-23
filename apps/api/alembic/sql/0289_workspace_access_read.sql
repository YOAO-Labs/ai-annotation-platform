-- Add a read-only serialization mode without changing the frozen key_share or
-- update modes. The project access read phase reuses this complete row shape.
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
       OR p_lock_mode NOT IN ('key_share', 'update', 'read') THEN
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
-- Fixed Workspace-owned fact phases for the current Project access resolver.
CREATE OR REPLACE FUNCTION aap_workspace_v1.project_access_step(
    p_actor_id uuid,
    p_project_id uuid,
    p_phase text
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
    v_facts jsonb;
    v_actor jsonb;
    v_project_row_json text;
    v_member_rows_json text;
    v_sqlstate text;
BEGIN
    IF p_actor_id IS NULL
       OR p_project_id IS NULL
       OR p_phase IS NULL
       OR p_phase NOT IN (
           'read_project_row',
           'lock_actor_share',
           'lock_project_key_share',
           'lock_member_share'
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace Project access step';
    END IF;

    IF p_phase = 'read_project_row' THEN
        v_project_row_json :=
            aap_workspace_configuration_v1._class_rename_project_row(
                p_project_id, 'read'
            );
        v_facts := pg_catalog.jsonb_build_object(
            'row_json', CASE
                WHEN v_project_row_json IS NULL THEN NULL
                ELSE pg_catalog.to_jsonb(v_project_row_json)
            END
        );
    ELSIF p_phase = 'lock_actor_share' THEN
        BEGIN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                p_actor_id, true, 'account_busy'
            );
        EXCEPTION WHEN lock_not_available
                     OR serialization_failure
                     OR deadlock_detected THEN
            GET STACKED DIAGNOSTICS v_sqlstate = RETURNED_SQLSTATE;
            RAISE EXCEPTION USING
                ERRCODE = v_sqlstate,
                MESSAGE = 'Workspace Project access lock is busy',
                HINT = 'aap.workspace.project_access.account_busy';
        END;

        IF v_actor IS NOT NULL
           AND (
               pg_catalog.jsonb_typeof(v_actor) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_actor->'id') IS DISTINCT FROM 'string'
               OR v_actor->>'id' IS DISTINCT FROM p_actor_id::text
               OR pg_catalog.jsonb_typeof(v_actor->'role') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_actor->'is_active') IS DISTINCT FROM 'boolean'
           ) THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace Project access actor fact';
        END IF;

        v_facts := pg_catalog.jsonb_build_object(
            'actor', CASE
                WHEN v_actor IS NULL THEN NULL
                ELSE pg_catalog.jsonb_build_object(
                    'actor_id', p_actor_id,
                    'platform_role', v_actor->'role',
                    'is_active', v_actor->'is_active'
                )
            END
        );
    ELSIF p_phase = 'lock_project_key_share' THEN
        BEGIN
            v_project_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    p_project_id, 'key_share'
                );
        EXCEPTION WHEN lock_not_available
                     OR serialization_failure
                     OR deadlock_detected THEN
            GET STACKED DIAGNOSTICS v_sqlstate = RETURNED_SQLSTATE;
            RAISE EXCEPTION USING
                ERRCODE = v_sqlstate,
                MESSAGE = 'Workspace Project access lock is busy',
                HINT = 'aap.workspace.project_access.project_busy';
        END;

        v_facts := pg_catalog.jsonb_build_object(
            'row_json', CASE
                WHEN v_project_row_json IS NULL THEN NULL
                ELSE pg_catalog.to_jsonb(v_project_row_json)
            END
        );
    ELSE
        BEGIN
            v_member_rows_json :=
                aap_workspace_configuration_v1._class_rename_member_rows(
                    p_project_id, p_actor_id
                );
        EXCEPTION WHEN lock_not_available
                     OR serialization_failure
                     OR deadlock_detected THEN
            GET STACKED DIAGNOSTICS v_sqlstate = RETURNED_SQLSTATE;
            RAISE EXCEPTION USING
                ERRCODE = v_sqlstate,
                MESSAGE = 'Workspace Project access lock is busy',
                HINT = 'aap.workspace.project_access.membership_busy';
        END;

        IF v_member_rows_json IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace Project access membership fact';
        END IF;
        v_facts := pg_catalog.jsonb_build_object(
            'rows_json', v_member_rows_json
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-project-access-step/v1',
        'actor_id', p_actor_id,
        'project_id', p_project_id,
        'phase', p_phase,
        'facts', v_facts
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_v1.project_access_step(uuid, uuid, text)
    FROM PUBLIC;
-- aap-statement-break
COMMENT ON FUNCTION
    aap_workspace_v1.project_access_step(uuid, uuid, text)
    IS 'Returns one fixed Project access phase fact; caller orders phases and decodes each fact before continuing.';
