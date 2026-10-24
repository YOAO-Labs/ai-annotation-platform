CREATE FUNCTION aap_workspace_v1.create_media_seed_project(
    p_project_id uuid,
    p_display_id text,
    p_name text,
    p_owner_id uuid,
    p_kind text,
    p_tool_bindings jsonb
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
    v_type_label text;
    v_type_key text;
    v_data_type text;
    v_ai_enabled boolean;
    v_ai_interactive_enabled boolean;
BEGIN
    IF p_project_id IS NULL
       OR p_display_id IS NULL
       OR p_name IS NULL
       OR p_owner_id IS NULL
       OR p_kind IS NULL
       OR pg_catalog.jsonb_typeof(p_tool_bindings) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    CASE p_kind
        WHEN 'coco8' THEN
            v_type_label := '图像 · 目标检测';
            v_type_key := 'image-det';
            v_data_type := 'image';
            v_ai_enabled := true;
            v_ai_interactive_enabled := true;
        WHEN 'video' THEN
            v_type_label := '视频 · 时序追踪';
            v_type_key := 'video-track';
            v_data_type := 'video';
            v_ai_enabled := true;
            v_ai_interactive_enabled := true;
        WHEN 'ocr' THEN
            v_type_label := '图像 · OCR';
            v_type_key := 'image-ocr';
            v_data_type := 'image';
            v_ai_enabled := true;
            v_ai_interactive_enabled := true;
        WHEN 'large_image' THEN
            v_type_label := '图像 · 实例分割';
            v_type_key := 'image-seg';
            v_data_type := 'image';
            v_ai_enabled := false;
            v_ai_interactive_enabled := false;
        ELSE
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END CASE;

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
        p_project_id, p_display_id, p_name, p_owner_id, v_type_label, v_type_key,
        v_data_type, 'in_progress', v_ai_enabled, p_tool_bindings, '{}'::jsonb,
        'sequence', 1, false, 0.7, 0.35, 0.25, v_ai_interactive_enabled, true,
        '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
        false, false, 30
    );

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-media-seed-project/v1',
        'kind', p_kind,
        'display_id', p_display_id,
        'project_id', p_project_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.write_seed_project_projection(
    p_project_id uuid,
    p_patch jsonb
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
    v_column text;
    v_assignments text[] := ARRAY[]::text[];
    v_updated_at_expression text;
    v_updated_at timestamptz;
    v_row_count integer;
BEGIN
    IF p_project_id IS NULL
       OR p_patch IS NULL
       OR pg_catalog.jsonb_typeof(p_patch) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    IF (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_patch)) = 0
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.jsonb_object_keys(p_patch) AS supplied(name)
           WHERE supplied.name NOT IN (
               'name', 'type_label', 'type_key', 'data_type', 'owner_id',
               'status', 'ai_enabled', 'ai_interactive_enabled',
               'raster_mask_native_editing_enabled', 'tool_bindings',
               'created_at', 'updated_at', 'ml_backend_pool_id',
               'preannotate_pipeline'
           )
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    IF (p_patch ? 'name'
        AND pg_catalog.jsonb_typeof(p_patch -> 'name') IS DISTINCT FROM 'string')
       OR (p_patch ? 'type_label'
           AND pg_catalog.jsonb_typeof(p_patch -> 'type_label') IS DISTINCT FROM 'string')
       OR (p_patch ? 'type_key'
           AND pg_catalog.jsonb_typeof(p_patch -> 'type_key') IS DISTINCT FROM 'string')
       OR (p_patch ? 'data_type'
           AND pg_catalog.jsonb_typeof(p_patch -> 'data_type') IS DISTINCT FROM 'string')
       OR (p_patch ? 'owner_id'
           AND pg_catalog.jsonb_typeof(p_patch -> 'owner_id') IS DISTINCT FROM 'string')
       OR (p_patch ? 'status'
           AND pg_catalog.jsonb_typeof(p_patch -> 'status') IS DISTINCT FROM 'string')
       OR (p_patch ? 'ai_enabled'
           AND pg_catalog.jsonb_typeof(p_patch -> 'ai_enabled') IS DISTINCT FROM 'boolean')
       OR (p_patch ? 'ai_interactive_enabled'
           AND pg_catalog.jsonb_typeof(p_patch -> 'ai_interactive_enabled')
                IS DISTINCT FROM 'boolean')
       OR (p_patch ? 'raster_mask_native_editing_enabled'
           AND pg_catalog.jsonb_typeof(
                   p_patch -> 'raster_mask_native_editing_enabled'
               ) IS DISTINCT FROM 'boolean')
       OR (p_patch ? 'tool_bindings'
           AND pg_catalog.jsonb_typeof(p_patch -> 'tool_bindings') IS DISTINCT FROM 'object')
       OR (p_patch ? 'created_at'
           AND pg_catalog.jsonb_typeof(p_patch -> 'created_at') IS DISTINCT FROM 'string')
       OR (p_patch ? 'updated_at'
           AND pg_catalog.jsonb_typeof(p_patch -> 'updated_at') IS DISTINCT FROM 'string')
       OR (p_patch ? 'ml_backend_pool_id'
           AND pg_catalog.jsonb_typeof(p_patch -> 'ml_backend_pool_id')
               NOT IN ('string', 'null'))
       OR (p_patch ? 'preannotate_pipeline'
           AND pg_catalog.jsonb_typeof(p_patch -> 'preannotate_pipeline')
               NOT IN ('array', 'null')) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    FOREACH v_column IN ARRAY ARRAY[
        'name',
        'type_label',
        'type_key',
        'data_type',
        'owner_id',
        'status',
        'ai_enabled',
        'ai_interactive_enabled',
        'raster_mask_native_editing_enabled',
        'tool_bindings',
        'created_at',
        'ml_backend_pool_id',
        'preannotate_pipeline'
    ] LOOP
        IF p_patch ? v_column THEN
            v_assignments := pg_catalog.array_append(
                v_assignments,
                pg_catalog.format(
                    '%I = (pg_catalog.jsonb_populate_record('
                    'NULL::public.projects, $2)).%I',
                    v_column,
                    v_column
                )
            );
        END IF;
    END LOOP;

    IF p_patch ? 'updated_at' THEN
        v_updated_at_expression :=
            '(pg_catalog.jsonb_populate_record('
            'NULL::public.projects, $2)).updated_at';
    ELSE
        v_updated_at_expression := 'pg_catalog.now()';
    END IF;
    v_assignments := pg_catalog.array_append(
        v_assignments,
        pg_catalog.format('updated_at = %s', v_updated_at_expression)
    );

    EXECUTE pg_catalog.format(
        'UPDATE public.projects AS project SET %s '
        'WHERE project.id = $1 RETURNING project.updated_at',
        pg_catalog.array_to_string(v_assignments, ', ')
    )
    INTO v_updated_at
    USING p_project_id, p_patch;
    GET DIAGNOSTICS v_row_count = ROW_COUNT;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-media-seed-project/v1',
        'project_id', p_project_id,
        'found', v_row_count > 0,
        'updated_at', v_updated_at
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_v1.write_seed_project_projection(uuid,jsonb)
    FROM PUBLIC;
