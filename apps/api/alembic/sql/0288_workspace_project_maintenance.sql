-- Operational callers retain their existing authorization and transaction.
CREATE FUNCTION aap_workspace_v1.read_import_project(p_display_id text)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project_id uuid;
BEGIN
    IF p_display_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    SELECT project.id INTO v_project_id
    FROM public.projects AS project
    WHERE project.display_id = p_display_id;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-project-maintenance/v1',
        'display_id', p_display_id,
        'project_id', v_project_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.read_import_project(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.create_import_project(
    p_project_id uuid, p_display_id text, p_name text,
    p_owner_id uuid, p_tool_bindings jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_project_id IS NULL OR p_display_id IS NULL OR p_name IS NULL
       OR p_owner_id IS NULL
       OR pg_catalog.jsonb_typeof(p_tool_bindings) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    -- Preserve ORM client defaults and the operator's stable display ID.
    INSERT INTO public.projects (
        id, display_id, name, owner_id, type_label, type_key, data_type,
        status, ai_enabled, tool_bindings, label_config, sampling,
        maximum_annotations, show_overlap_first, iou_dedup_threshold,
        box_threshold, text_threshold, ai_interactive_enabled,
        raster_mask_native_editing_enabled, mask_qc_config,
        point_cloud_quality_config, rendering_config, video_sampling,
        video_collaboration, scene_mode, prefer_same_scene_continuation,
        scene_continuation_window_min
    ) VALUES (
        p_project_id, p_display_id, p_name, p_owner_id, '点云检测', 'lidar', 'lidar',
        'in_progress', false, p_tool_bindings, '{}'::jsonb, 'sequence',
        1, false, 0.7, 0.35, 0.25, true, true, '{}'::jsonb,
        '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, true, true, 30
    );

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-project-maintenance/v1',
        'display_id', p_display_id,
        'project_id', p_project_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.write_point_cloud_quality_config(
    p_project_id uuid, p_config jsonb
)
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
    v_updated_at timestamptz;
BEGIN
    IF p_project_id IS NULL
       OR pg_catalog.jsonb_typeof(p_config) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    UPDATE public.projects AS project
    SET point_cloud_quality_config = p_config, updated_at = pg_catalog.now()
    WHERE project.id = p_project_id
      AND project.point_cloud_quality_config IS DISTINCT FROM p_config
    RETURNING project.updated_at INTO v_updated_at;

    IF NOT FOUND THEN
        SELECT project.updated_at INTO v_updated_at
        FROM public.projects AS project WHERE project.id = p_project_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'Workspace project not found';
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-project-maintenance/v1',
        'project_id', p_project_id,
        'updated_at', v_updated_at
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.write_point_cloud_quality_config(uuid,jsonb)
    FROM PUBLIC;
