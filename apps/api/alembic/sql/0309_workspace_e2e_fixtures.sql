CREATE SCHEMA aap_workspace_fixture_v1;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_workspace_fixture_v1 FROM PUBLIC;
-- aap-statement-break
CREATE VIEW aap_workspace_fixture_v1.templates
WITH (security_barrier = true)
AS
SELECT template.id, template.display_id
FROM public.project_templates AS template
WHERE pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) = '_test'
   OR pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) = '_e2e';
-- aap-statement-break
REVOKE ALL ON TABLE aap_workspace_fixture_v1.templates FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_fixture_v1.create_project(p_project jsonb)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_project_id uuid;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;

    IF p_project IS NULL
       OR pg_catalog.jsonb_typeof(p_project) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid E2E Project record';
    END IF;
    IF NOT (p_project ?& ARRAY[
           'id', 'display_id', 'name', 'type_label', 'type_key', 'data_type',
           'owner_id', 'tool_bindings', 'scene_mode', 'ai_enabled'
       ])
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.jsonb_object_keys(p_project) AS field(name)
           WHERE field.name <> ALL (ARRAY[
               'id', 'display_id', 'name', 'type_label', 'type_key', 'data_type',
               'owner_id', 'tool_bindings', 'scene_mode', 'ai_enabled'
           ])
       )
       OR pg_catalog.jsonb_typeof(p_project -> 'id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_project -> 'display_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_project -> 'name') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_project -> 'type_label') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_project -> 'type_key') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_project -> 'data_type') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_project -> 'owner_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_project -> 'tool_bindings') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_project -> 'scene_mode') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_project -> 'ai_enabled') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid E2E Project record';
    END IF;

    INSERT INTO public.projects (
        id, display_id, name, type_label, type_key, data_type, owner_id,
        status, ai_enabled, default_variants, preannotate_pipeline,
        label_config, tool_bindings, sampling,
        maximum_annotations, show_overlap_first, iou_dedup_threshold,
        box_threshold, text_threshold, ai_interactive_enabled,
        raster_mask_native_editing_enabled, mask_qc_config,
        point_cloud_quality_config, rendering_config, annotation_guide,
        guide_assets, video_sampling, video_collaboration, scene_mode,
        prefer_same_scene_continuation, scene_continuation_window_min, due_date
    ) VALUES (
        (p_project ->> 'id')::uuid,
        p_project ->> 'display_id',
        p_project ->> 'name',
        p_project ->> 'type_label',
        p_project ->> 'type_key',
        p_project ->> 'data_type',
        (p_project ->> 'owner_id')::uuid,
        'in_progress',
        (p_project ->> 'ai_enabled')::boolean,
        '{}'::jsonb,
        NULL,
        '{}'::jsonb,
        p_project -> 'tool_bindings',
        'sequence',
        1,
        false,
        0.7,
        0.35,
        0.25,
        true,
        true,
        '{}'::jsonb,
        '{}'::jsonb,
        '{}'::jsonb,
        NULL,
        '[]'::jsonb,
        '{}'::jsonb,
        '{}'::jsonb,
        (p_project ->> 'scene_mode')::boolean,
        false,
        30,
        NULL
    )
    RETURNING id INTO v_project_id;
    RETURN v_project_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_fixture_v1.create_project(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_fixture_v1.create_templates(p_templates jsonb)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_row_count integer;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;

    IF p_templates IS NULL
       OR pg_catalog.jsonb_typeof(p_templates) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid E2E Template records';
    END IF;
    IF EXISTS (
           SELECT 1
           FROM pg_catalog.jsonb_array_elements(p_templates) AS item(value)
           WHERE CASE
               WHEN pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'object' THEN true
               ELSE
                   NOT (item.value ?& ARRAY[
                       'id', 'display_id', 'name', 'description', 'type_label',
                       'type_key', 'data_type', 'tool_bindings', 'label_config',
                       'scope', 'created_by', 'source_project_id'
                   ])
                   OR EXISTS (
                       SELECT 1
                       FROM pg_catalog.jsonb_object_keys(item.value) AS field(name)
                       WHERE field.name <> ALL (ARRAY[
                           'id', 'display_id', 'name', 'description', 'type_label',
                           'type_key', 'data_type', 'tool_bindings', 'label_config',
                           'scope', 'created_by', 'source_project_id'
                       ])
                   )
                   OR pg_catalog.jsonb_typeof(item.value -> 'id') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(item.value -> 'display_id') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(item.value -> 'name') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(item.value -> 'description') NOT IN ('string', 'null')
                   OR pg_catalog.jsonb_typeof(item.value -> 'type_label') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(item.value -> 'type_key') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(item.value -> 'data_type') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(item.value -> 'tool_bindings') IS DISTINCT FROM 'object'
                   OR pg_catalog.jsonb_typeof(item.value -> 'label_config') IS DISTINCT FROM 'object'
                   OR pg_catalog.jsonb_typeof(item.value -> 'scope') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(item.value -> 'created_by') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(item.value -> 'source_project_id') NOT IN ('string', 'null')
           END
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid E2E Template records';
    END IF;

    INSERT INTO public.project_templates (
        id, display_id, name, description, type_label, type_key, data_type,
        tool_bindings, label_config, ai_enabled, sampling, maximum_annotations,
        show_overlap_first, iou_dedup_threshold, box_threshold, text_threshold,
        rendering_config, annotation_guide, scope, organization_id, created_by,
        source_project_id
    )
    SELECT
        template.id,
        template.display_id,
        template.name,
        template.description,
        template.type_label,
        template.type_key,
        template.data_type,
        template.tool_bindings,
        template.label_config,
        false,
        'sequence',
        1,
        false,
        0.7,
        0.35,
        0.25,
        '{}'::jsonb,
        NULL,
        template.scope,
        NULL,
        template.created_by,
        template.source_project_id
    FROM pg_catalog.jsonb_to_recordset(p_templates) AS template (
        id uuid,
        display_id text,
        name text,
        description text,
        type_label text,
        type_key text,
        data_type text,
        tool_bindings jsonb,
        label_config jsonb,
        scope text,
        created_by uuid,
        source_project_id uuid
    );
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_fixture_v1.create_templates(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_fixture_v1.guard_project_cleanup(p_project_ids uuid[])
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
    v_project_id uuid;
    v_guard jsonb;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;
    IF p_project_ids IS NULL OR pg_catalog.array_position(p_project_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid E2E Project cleanup scope';
    END IF;

    FOR v_project_id IN
        SELECT project.id
        FROM public.projects AS project
        WHERE project.id = ANY (p_project_ids)
        ORDER BY project.id
        FOR UPDATE OF project NOWAIT
    LOOP
        v_guard := aap_labeling_v1.guard_project_video_deletion(v_project_id);
        IF v_guard IS NULL
           OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-video-deletion-guard/v1' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'project video deletion guard contract mismatch';
        END IF;
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            IF v_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
               OR v_guard -> 'error' IS DISTINCT FROM
                    pg_catalog.jsonb_build_object('code', 'video_execution_not_retired')
               OR (v_guard - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'project video deletion guard contract mismatch';
            END IF;
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-workspace-e2e-fixtures/v1',
                'ok', false,
                'reason', 'video_execution_not_retired'
            );
        END IF;
        IF v_guard -> 'data' IS DISTINCT FROM '{}'::jsonb
           OR (v_guard - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'project video deletion guard contract mismatch';
        END IF;
    END LOOP;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-e2e-fixtures/v1',
        'ok', true,
        'reason', NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_fixture_v1.guard_project_cleanup(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_fixture_v1.delete_projects(p_project_ids uuid[])
RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_guard jsonb;
    v_row_count integer;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;
    IF p_project_ids IS NULL OR pg_catalog.array_position(p_project_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid E2E Project cleanup scope';
    END IF;

    v_guard := aap_workspace_fixture_v1.guard_project_cleanup(p_project_ids);
    IF v_guard IS NULL
       OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-workspace-e2e-fixtures/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Workspace E2E Project cleanup guard contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
           OR v_guard ->> 'reason' IS DISTINCT FROM 'video_execution_not_retired'
           OR (v_guard - ARRAY['schema', 'ok', 'reason']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace E2E Project cleanup guard contract mismatch';
        END IF;
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'video_execution_not_retired';
    END IF;
    IF v_guard -> 'reason' IS DISTINCT FROM 'null'::jsonb
       OR (v_guard - ARRAY['schema', 'ok', 'reason']) <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Workspace E2E Project cleanup guard contract mismatch';
    END IF;

    DELETE FROM public.projects
    WHERE id = ANY (p_project_ids);
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_fixture_v1.delete_projects(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_fixture_v1.delete_templates(
    p_shared boolean,
    p_display_ids text[]
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_row_count integer;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;
    IF p_shared IS NULL
       OR p_display_ids IS NULL
       OR pg_catalog.array_position(p_display_ids, NULL::text) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid E2E Template cleanup scope';
    END IF;

    IF p_shared THEN
        DELETE FROM public.project_templates
        WHERE display_id LIKE 'TPL-E2E-FILTER-%';
    ELSE
        DELETE FROM public.project_templates
        WHERE display_id = ANY (p_display_ids);
    END IF;
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_fixture_v1.delete_templates(boolean, text[])
    FROM PUBLIC;
