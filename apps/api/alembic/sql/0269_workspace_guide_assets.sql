-- Private unlocked full-row facts required by legacy Guide phase boundaries.
CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1._guide_asset_read_project_row(
    p_project_id uuid
)
RETURNS text
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_row_json text;
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid guide-assets Project read request';
    END IF;

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

    RETURN v_row_json;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_configuration_v1._guide_asset_read_project_row(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1._guide_asset_read_member_rows(
    p_project_id uuid,
    p_actor_id uuid
)
RETURNS text
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_rows_json text;
BEGIN
    IF p_project_id IS NULL OR p_actor_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid guide-assets ProjectMember read request';
    END IF;

    WITH selected_members AS MATERIALIZED (
        SELECT member.id,
               member.project_id,
               member.user_id,
               member.role::text AS role,
               member.assigned_by,
               member.assigned_at,
               member.version,
               member.updated_at,
               member.weekly_target
        FROM public.project_members AS member
        WHERE member.project_id = p_project_id
          AND member.user_id = p_actor_id
    )
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'id', member.id,
                'project_id', member.project_id,
                'user_id', member.user_id,
                'role', member.role,
                'assigned_by', member.assigned_by,
                'assigned_at', pg_catalog.to_jsonb(member.assigned_at),
                'version', member.version,
                'updated_at', pg_catalog.to_jsonb(member.updated_at),
                'weekly_target', member.weekly_target
            )
        ),
        '[]'::jsonb
    )::text
    INTO v_rows_json
    FROM selected_members AS member;

    RETURN v_rows_json;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_configuration_v1._guide_asset_read_member_rows(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1.configuration_step(
    p_request jsonb,
    p_action text,
    p_args jsonb,
    p_column_names text[],
    p_column_values text[]
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_action text;
    v_operation_id uuid;
    v_actor_id uuid;
    v_target_id uuid;
    v_selectors jsonb;
    v_request_hash text;
    v_txid bigint;
    v_actor jsonb;
    v_project jsonb;
    v_source_project jsonb;
    v_template jsonb;
    v_project_access jsonb;
    v_source_project_access jsonb;
    v_facts jsonb;
    v_replay jsonb;
    v_receipt public.workspace_configuration_operation_receipts%ROWTYPE;
    v_state public.workspace_configuration_operation_states%ROWTYPE;
    v_state_found boolean;
    v_active boolean;
    v_authorized boolean;
    v_is_creator boolean;
    v_scope text;
    v_organization_id uuid;
    v_id uuid;
    v_resource_id uuid;
    v_display_id text;
    v_registry_id uuid;
    v_pool_id uuid;
    v_project_id uuid;
    v_dataset_id uuid;
    v_item_ids uuid[];
    v_dataset_ids uuid[];
    v_media_ids uuid[];
    v_dataset_reply jsonb;
    v_media_reply jsonb;
    v_backend_reply jsonb;
    v_support_reply jsonb;
    v_summary jsonb;
    v_summaries jsonb;
    v_output jsonb;
    v_result jsonb;
    v_detail jsonb;
    v_headers jsonb;
    v_rows integer;
    v_name text;
    v_value text;
    v_cast text;
    v_table text;
    v_sql text;
    v_columns_sql text;
    v_values_sql text;
    v_assignments_sql text;
    v_column_count integer;
    v_index integer;
    v_action_json jsonb;
    v_source_pool_id uuid;
    v_found boolean;
    v_enabled_pool_id uuid;
    v_authority_actor jsonb;
    v_authority_project jsonb;
    v_authority_source_project jsonb;
    v_authority_template jsonb;
    v_authority_project_access jsonb;
    v_authority_source_access jsonb;
    v_autoflush boolean;
    v_lock_busy boolean;
    v_current_pool_id uuid;
    v_frozen_template jsonb;
    v_frozen_visible boolean;
    v_frozen_editable boolean;
    v_state_columns text[];
    v_class_rename_row_json text;
    v_class_rename_member_rows_json text;
    v_class_rename_old_name text;
    v_class_rename_new_name text;
    v_class_rename_tool_bindings_ascii text;
    v_class_rename_tool_bindings jsonb;
    v_guide_row_json text;
    v_guide_rows_json text;
    v_guide_assets_ascii text;
    v_guide_assets jsonb;
    v_guide_actor_exists boolean;
    v_uuid_pattern constant text :=
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
BEGIN
    IF p_action IS NULL OR p_action NOT IN (
        'begin', 'backend', 'source_pool', 'video_annotations',
        'video_trackers', 'video_items', 'video_media', 'task_count',
        'lock_config', 'datasets', 'dataset_scenes', 'authority', 'allocate',
        'insert', 'enable_backend', 'binding', 'update', 'delete', 'finish',
        'class_rename_prepare', 'class_rename_member',
        'class_rename_lock', 'class_rename_apply',
        'guide_account_exists', 'guide_read_project', 'guide_actor_status',
        'guide_read_member', 'guide_shared', 'guide_member', 'guide_lock',
        'guide_apply', 'guide_refresh'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration action';
    END IF;
    v_action := p_action;
    IF NOT aap_workspace_configuration_v1._has_exact_keys(
        p_request,
        ARRAY[
            'schema', 'operation_id', 'operation', 'actor_id', 'target_id',
            'selectors', 'payload_ascii'
        ]::text[]
    ) OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
      OR p_request->>'schema' IS DISTINCT FROM 'aap-workspace-configuration/v1'
      OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
      OR (p_request->>'operation_id') !~* v_uuid_pattern
      OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
      OR p_request->>'operation' NOT IN (
          'create_project', 'update_project', 'create_template',
          'update_template', 'delete_template', 'duplicate_template',
          'rename_project_class', 'guide_asset_upload_init',
          'guide_asset_upload_complete', 'guide_asset_delete', 'guide_asset_sign'
      )
      OR pg_catalog.jsonb_typeof(p_request->'actor_id') IS DISTINCT FROM 'string'
      OR (p_request->>'actor_id') !~* v_uuid_pattern
      OR pg_catalog.jsonb_typeof(p_request->'target_id') NOT IN ('null', 'string')
      OR (
          pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
          AND (p_request->>'target_id') !~* v_uuid_pattern
      )
      OR pg_catalog.jsonb_typeof(p_request->'payload_ascii') IS DISTINCT FROM 'string'
      OR pg_catalog.octet_length(pg_catalog.convert_to(
          p_request->>'payload_ascii', 'UTF8'
      )) <> pg_catalog.char_length(p_request->>'payload_ascii')
      OR NOT aap_workspace_configuration_v1._has_exact_keys(
          p_request->'selectors',
          ARRAY[
              'source_project_id', 'template_id', 'ml_backend_source_id',
              'ml_backend_id', 'ml_backend_present', 'scope', 'scope_present',
              'organization_id', 'organization_present'
          ]::text[]
      ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration request';
    END IF;

    v_operation := p_request->>'operation';
    v_operation_id := (p_request->>'operation_id')::uuid;
    v_actor_id := (p_request->>'actor_id')::uuid;
    v_target_id := CASE
        WHEN pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
        THEN (p_request->>'target_id')::uuid
        ELSE NULL
    END;
    v_selectors := p_request->'selectors';
    IF (
        pg_catalog.jsonb_typeof(v_selectors->'source_project_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'source_project_id') = 'string'
            AND (v_selectors->>'source_project_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'template_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'template_id') = 'string'
            AND (v_selectors->>'template_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'ml_backend_source_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'ml_backend_source_id') = 'string'
            AND (v_selectors->>'ml_backend_source_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'ml_backend_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'ml_backend_id') = 'string'
            AND (v_selectors->>'ml_backend_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'ml_backend_present') IS DISTINCT FROM 'boolean'
        OR pg_catalog.jsonb_typeof(v_selectors->'scope') NOT IN ('null', 'string')
        OR pg_catalog.jsonb_typeof(v_selectors->'scope_present') IS DISTINCT FROM 'boolean'
        OR pg_catalog.jsonb_typeof(v_selectors->'organization_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'organization_id') = 'string'
            AND (v_selectors->>'organization_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'organization_present') IS DISTINCT FROM 'boolean'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration selectors';
    END IF;
    IF (v_operation IN ('create_project', 'create_template') AND v_target_id IS NOT NULL)
       OR (v_operation NOT IN ('create_project', 'create_template') AND v_target_id IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration target binding';
    END IF;
    IF v_operation = 'rename_project_class' AND (
        v_selectors->'source_project_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'template_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'ml_backend_source_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'ml_backend_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'ml_backend_present' IS DISTINCT FROM 'false'::jsonb
        OR v_selectors->'scope' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'scope_present' IS DISTINCT FROM 'false'::jsonb
        OR v_selectors->'organization_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'organization_present' IS DISTINCT FROM 'false'::jsonb
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid class rename selectors';
    END IF;
    IF v_operation IN (
        'guide_asset_upload_init', 'guide_asset_upload_complete',
        'guide_asset_delete', 'guide_asset_sign'
    ) AND (
        v_selectors->'source_project_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'template_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'ml_backend_source_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'ml_backend_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'ml_backend_present' IS DISTINCT FROM 'false'::jsonb
        OR v_selectors->'scope' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'scope_present' IS DISTINCT FROM 'false'::jsonb
        OR v_selectors->'organization_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'organization_present' IS DISTINCT FROM 'false'::jsonb
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid guide-assets selectors';
    END IF;
    IF p_args IS NULL OR pg_catalog.jsonb_typeof(p_args) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration step arguments';
    END IF;
    IF p_action = 'begin' AND NOT aap_workspace_configuration_v1._has_exact_keys(
        p_args, ARRAY[]::text[]
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid begin arguments';
    ELSIF p_action IN ('backend', 'enable_backend')
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['registry_id']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend arguments';
    ELSIF p_action = 'source_pool'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['pool_id']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid source-pool arguments';
    ELSIF p_action = 'class_rename_apply'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['old_name', 'new_name', 'tool_bindings_ascii']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid class rename apply arguments';
    ELSIF p_action = 'guide_apply'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['guide_assets_ascii']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid guide-assets apply arguments';
    ELSIF p_action IN (
        'video_annotations', 'video_trackers', 'video_items', 'task_count',
        'datasets', 'allocate', 'insert', 'delete', 'finish',
        'class_rename_prepare', 'class_rename_member', 'class_rename_lock',
        'guide_account_exists', 'guide_read_project', 'guide_actor_status',
        'guide_read_member', 'guide_shared', 'guide_member', 'guide_lock',
        'guide_refresh'
    ) AND NOT aap_workspace_configuration_v1._has_exact_keys(
        p_args, ARRAY[]::text[]
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid empty arguments';
    ELSIF p_action = 'video_media'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['item_ids']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid video-media arguments';
    ELSIF p_action = 'dataset_scenes'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['dataset_id']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset-scenes arguments';
    ELSIF p_action = 'lock_config'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['kind']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid lock-config arguments';
    ELSIF p_action = 'authority'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['autoflush']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid authority arguments';
    ELSIF p_action = 'binding'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['pool_id']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid binding arguments';
    ELSIF p_action IN ('insert', 'update')
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY[]::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid write arguments';
    ELSIF p_action IN ('update', 'insert')
       AND (p_column_names IS NULL OR p_column_values IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'missing configuration columns';
    END IF;

    IF p_action IN ('backend', 'enable_backend') THEN
        IF pg_catalog.jsonb_typeof(p_args->'registry_id') IS DISTINCT FROM 'string'
           OR (p_args->>'registry_id') !~* v_uuid_pattern THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend selector';
        END IF;
    ELSIF p_action = 'source_pool' THEN
        IF pg_catalog.jsonb_typeof(p_args->'pool_id') IS DISTINCT FROM 'string'
           OR (p_args->>'pool_id') !~* v_uuid_pattern THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid source-pool selector';
        END IF;
    ELSIF p_action = 'dataset_scenes' THEN
        IF pg_catalog.jsonb_typeof(p_args->'dataset_id') IS DISTINCT FROM 'string'
           OR (p_args->>'dataset_id') !~* v_uuid_pattern THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset selector';
        END IF;
    ELSIF p_action = 'lock_config' THEN
        IF pg_catalog.jsonb_typeof(p_args->'kind') IS DISTINCT FROM 'string'
           OR p_args->>'kind' NOT IN ('mask_qc', 'point_cloud') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid project config lock kind';
        END IF;
    ELSIF p_action = 'authority' THEN
        IF pg_catalog.jsonb_typeof(p_args->'autoflush') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid authority autoflush';
        END IF;
    ELSIF p_action = 'binding' THEN
        IF pg_catalog.jsonb_typeof(p_args->'pool_id') NOT IN ('null', 'string')
           OR (pg_catalog.jsonb_typeof(p_args->'pool_id') = 'string'
               AND (p_args->>'pool_id') !~* v_uuid_pattern) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid binding pool';
        END IF;
    ELSIF p_action = 'video_media' THEN
        IF pg_catalog.jsonb_typeof(p_args->'item_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_args->'item_ids') > 500
           OR EXISTS (
               SELECT 1
               FROM pg_catalog.jsonb_array_elements(p_args->'item_ids') AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                  OR item.value #>> '{}' !~* v_uuid_pattern
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid video-media item ids';
        END IF;
    END IF;
    IF p_action = 'guide_apply' THEN
        IF pg_catalog.jsonb_typeof(
            p_args->'guide_assets_ascii'
        ) IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid guide-assets apply values';
        END IF;
        v_guide_assets_ascii := p_args->>'guide_assets_ascii';
        IF pg_catalog.octet_length(pg_catalog.convert_to(
            v_guide_assets_ascii, 'UTF8'
        )) <> pg_catalog.char_length(v_guide_assets_ascii) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'guide-assets JSON must be ASCII';
        END IF;
    ELSIF p_action = 'class_rename_apply' THEN
        IF pg_catalog.jsonb_typeof(p_args->'old_name') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_args->'new_name') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(
               p_args->'tool_bindings_ascii'
           ) IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid class rename apply values';
        END IF;
        v_class_rename_old_name := p_args->>'old_name';
        v_class_rename_new_name := p_args->>'new_name';
        v_class_rename_tool_bindings_ascii := p_args->>'tool_bindings_ascii';
        IF v_class_rename_old_name = ''
           OR v_class_rename_new_name = ''
           OR v_class_rename_old_name IS NOT DISTINCT FROM v_class_rename_new_name
           OR pg_catalog.octet_length(pg_catalog.convert_to(
               v_class_rename_tool_bindings_ascii, 'UTF8'
           )) <> pg_catalog.char_length(v_class_rename_tool_bindings_ascii) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid class rename apply values';
        END IF;
    END IF;

    IF p_action = 'update' OR p_action = 'insert' THEN
        v_column_count := pg_catalog.cardinality(p_column_names);
        IF v_column_count IS DISTINCT FROM pg_catalog.cardinality(p_column_values)
           OR pg_catalog.array_position(p_column_names, NULL::text) IS NOT NULL
           OR (
               v_column_count > 0
               AND pg_catalog.array_ndims(p_column_names) IS DISTINCT FROM 1
           ) OR (
               v_column_count > 0
               AND pg_catalog.array_ndims(p_column_values) IS DISTINCT FROM 1
           ) OR EXISTS (
               SELECT 1
               FROM pg_catalog.unnest(p_column_names) AS field(name)
               GROUP BY field.name
               HAVING pg_catalog.count(*) > 1
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid configuration column arrays';
        END IF;
    ELSIF pg_catalog.cardinality(COALESCE(p_column_names, ARRAY[]::text[])) <> 0
       OR pg_catalog.cardinality(COALESCE(p_column_values, ARRAY[]::text[])) <> 0 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unexpected configuration columns';
    END IF;

    IF p_action IN (
        'class_rename_prepare', 'class_rename_member',
        'class_rename_lock', 'class_rename_apply',
        'guide_account_exists', 'guide_read_project', 'guide_actor_status',
        'guide_read_member', 'guide_shared', 'guide_member', 'guide_lock',
        'guide_apply', 'guide_refresh'
    ) AND (
        p_column_names IS NULL
        OR pg_catalog.cardinality(p_column_names) IS DISTINCT FROM 0
        OR p_column_values IS NULL
        OR pg_catalog.cardinality(p_column_values) IS DISTINCT FROM 0
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'class rename actions do not accept generic columns';
    END IF;


    IF v_operation = 'rename_project_class' THEN
        IF p_action NOT IN (
            'class_rename_prepare', 'class_rename_member',
            'class_rename_lock', 'class_rename_apply'
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace class rename action';
        END IF;

        IF p_action = 'class_rename_prepare' THEN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_actor IS NULL
               OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer'),
                    false
                );
            END IF;
            IF v_actor->>'role' IS NULL
               OR v_actor->>'role' NOT IN (
                   'super_admin', 'project_admin', 'employee', 'viewer'
               ) THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('无效的认证凭据'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer'),
                    false
                );
            END IF;

            v_class_rename_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    v_target_id, 'key_share'
                );
            v_facts := pg_catalog.jsonb_build_object(
                'actor', pg_catalog.jsonb_build_object(
                    'actor_id', (v_actor->>'id')::uuid,
                    'platform_role', v_actor->'role',
                    'is_active', v_actor->'is_active'
                ),
                'row_json', CASE
                    WHEN v_class_rename_row_json IS NULL THEN NULL
                    ELSE pg_catalog.to_jsonb(v_class_rename_row_json)
                END
            );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action, v_facts
            );
        ELSIF p_action = 'class_rename_member' THEN
            v_class_rename_member_rows_json :=
                aap_workspace_configuration_v1._class_rename_member_rows(
                    v_target_id, v_actor_id
                );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request,
                p_action,
                pg_catalog.jsonb_build_object(
                    'rows_json', v_class_rename_member_rows_json
                )
            );
        ELSIF p_action = 'class_rename_lock' THEN
            v_class_rename_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    v_target_id, 'update'
                );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request,
                p_action,
                pg_catalog.jsonb_build_object(
                    'row_json', CASE
                        WHEN v_class_rename_row_json IS NULL THEN NULL
                        ELSE pg_catalog.to_jsonb(v_class_rename_row_json)
                    END
                )
            );
        ELSE
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_actor IS NULL
               OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer'),
                    false
                );
            END IF;
            IF v_actor->>'role' IS NULL
               OR v_actor->>'role' NOT IN (
                   'super_admin', 'project_admin', 'employee', 'viewer'
               ) THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('无效的认证凭据'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer'),
                    false
                );
            END IF;

            v_class_rename_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    v_target_id, 'key_share'
                );
            IF v_class_rename_row_json IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text),
                    NULL,
                    false
                );
            END IF;
            v_project := v_class_rename_row_json::jsonb;
            v_project_access :=
                aap_workspace_configuration_v1._configuration_project_access(
                    v_actor, v_project, true
                );
            IF v_project_access->>'code' = 'project_missing' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text),
                    NULL,
                    false
                );
            ELSIF v_project_access->>'code' = 'invalid_account_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text),
                    NULL,
                    false
                );
            ELSIF v_project_access->>'code' = 'invalid_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text),
                    NULL,
                    false
                );
            ELSIF v_project_access->>'code' = 'incompatible_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text),
                    NULL,
                    false
                );
            ELSIF COALESCE(
                (v_project_access->>'is_manager')::boolean, false
            ) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行'::text),
                    NULL,
                    false
                );
            END IF;

            v_class_rename_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    v_target_id, 'update'
                );
            IF v_class_rename_row_json IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text),
                    NULL,
                    false
                );
            END IF;

            BEGIN
                v_class_rename_tool_bindings :=
                    v_class_rename_tool_bindings_ascii::jsonb;
            EXCEPTION WHEN invalid_text_representation
                         OR untranslatable_character THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid class rename tool bindings';
            END;
            IF pg_catalog.jsonb_typeof(
                v_class_rename_tool_bindings
            ) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'class rename tool bindings must be an object';
            END IF;

            PERFORM aap_labeling_v1.rename_project_annotation_class(
                v_target_id,
                v_class_rename_old_name,
                v_class_rename_new_name
            );
            UPDATE public.projects
            SET tool_bindings = v_class_rename_tool_bindings,
                updated_at = pg_catalog.clock_timestamp()
            WHERE id = v_target_id;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Project class rename did not update one project';
            END IF;

            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action, '{}'::jsonb
            );
        END IF;
    ELSIF p_action IN (
        'class_rename_prepare', 'class_rename_member',
        'class_rename_lock', 'class_rename_apply'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'class rename action does not match operation';
    END IF;

    IF v_operation IN (
        'guide_asset_upload_init', 'guide_asset_upload_complete',
        'guide_asset_delete', 'guide_asset_sign'
    ) THEN
        IF (
            v_operation = 'guide_asset_upload_init'
            AND p_action NOT IN ('guide_shared', 'guide_member')
        ) OR (
            v_operation = 'guide_asset_upload_complete'
            AND p_action NOT IN (
                'guide_account_exists', 'guide_read_project',
                'guide_actor_status', 'guide_read_member',
                'guide_shared', 'guide_member', 'guide_lock',
                'guide_apply', 'guide_refresh'
            )
        ) OR (
            v_operation = 'guide_asset_delete'
            AND p_action NOT IN (
                'guide_shared', 'guide_member', 'guide_lock',
                'guide_apply', 'guide_refresh'
            )
        ) OR (
            v_operation = 'guide_asset_sign'
            AND p_action NOT IN ('guide_shared', 'guide_member')
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace guide-assets action';
        END IF;

        IF p_action = 'guide_account_exists' THEN
            SELECT EXISTS (
                SELECT 1
                FROM aap_identity_v1.read_actor_facts(v_actor_id) AS account
            )
            INTO v_guide_actor_exists;
            RETURN aap_workspace_configuration_v1._step_success(
                p_request,
                p_action,
                pg_catalog.jsonb_build_object('exists', v_guide_actor_exists)
            );
        ELSIF p_action = 'guide_read_project' THEN
            v_guide_row_json :=
                aap_workspace_configuration_v1._guide_asset_read_project_row(
                    v_target_id
                );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request,
                p_action,
                pg_catalog.jsonb_build_object(
                    'row_json', CASE
                        WHEN v_guide_row_json IS NULL THEN NULL
                        ELSE pg_catalog.to_jsonb(v_guide_row_json)
                    END
                )
            );
        ELSIF p_action = 'guide_actor_status' THEN
            SELECT pg_catalog.jsonb_build_object(
                'actor_id', account.actor_id,
                'platform_role', account.platform_role,
                'is_active', account.is_active
            )
            INTO v_actor
            FROM aap_identity_v1.read_actor_facts(v_actor_id) AS account;
            RETURN aap_workspace_configuration_v1._step_success(
                p_request,
                p_action,
                pg_catalog.jsonb_build_object('actor', v_actor)
            );
        ELSIF p_action = 'guide_read_member' THEN
            v_guide_rows_json :=
                aap_workspace_configuration_v1._guide_asset_read_member_rows(
                    v_target_id, v_actor_id
                );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request,
                p_action,
                pg_catalog.jsonb_build_object('rows_json', v_guide_rows_json)
            );
        ELSIF p_action = 'guide_shared' THEN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_actor IS NULL
               OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer'),
                    false
                );
            END IF;

            v_guide_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    v_target_id, 'key_share'
                );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request,
                p_action,
                pg_catalog.jsonb_build_object(
                    'actor', pg_catalog.jsonb_build_object(
                        'actor_id', (v_actor->>'id')::uuid,
                        'platform_role', v_actor->'role',
                        'is_active', v_actor->'is_active'
                    ),
                    'row_json', CASE
                        WHEN v_guide_row_json IS NULL THEN NULL
                        ELSE pg_catalog.to_jsonb(v_guide_row_json)
                    END
                )
            );
        ELSIF p_action = 'guide_member' THEN
            v_guide_rows_json :=
                aap_workspace_configuration_v1._class_rename_member_rows(
                    v_target_id, v_actor_id
                );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request,
                p_action,
                pg_catalog.jsonb_build_object('rows_json', v_guide_rows_json)
            );
        ELSIF p_action = 'guide_lock' THEN
            v_guide_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    v_target_id, 'update'
                );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request,
                p_action,
                pg_catalog.jsonb_build_object(
                    'row_json', CASE
                        WHEN v_guide_row_json IS NULL THEN NULL
                        ELSE pg_catalog.to_jsonb(v_guide_row_json)
                    END
                )
            );
        ELSIF p_action = 'guide_refresh' THEN
            v_guide_row_json :=
                aap_workspace_configuration_v1._guide_asset_read_project_row(
                    v_target_id
                );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request,
                p_action,
                pg_catalog.jsonb_build_object(
                    'row_json', CASE
                        WHEN v_guide_row_json IS NULL THEN NULL
                        ELSE pg_catalog.to_jsonb(v_guide_row_json)
                    END
                )
            );
        ELSE
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_actor IS NULL
               OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer'),
                    false
                );
            END IF;

            v_guide_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    v_target_id, 'key_share'
                );
            IF v_guide_row_json IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;
            v_project := v_guide_row_json::jsonb;
            v_project_access :=
                aap_workspace_configuration_v1._configuration_project_access(
                    v_actor, v_project, true
                );
            IF v_project_access->>'code' = 'project_missing' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            ELSIF v_project_access->>'code' = 'invalid_account_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                );
            ELSIF v_project_access->>'code' = 'invalid_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                );
            ELSIF v_project_access->>'code' = 'incompatible_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                );
            ELSIF COALESCE(
                (v_project_access->>'is_manager')::boolean, false
            ) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行'::text)
                );
            END IF;

            v_guide_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    v_target_id, 'update'
                );
            IF v_guide_row_json IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;

            BEGIN
                v_guide_assets := v_guide_assets_ascii::jsonb;
            EXCEPTION WHEN invalid_text_representation
                         OR untranslatable_character THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid guide-assets JSON';
            END;
            IF pg_catalog.jsonb_typeof(v_guide_assets) IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'guide-assets JSON must be an array';
            END IF;

            UPDATE public.projects
            SET guide_assets = v_guide_assets,
                updated_at = pg_catalog.clock_timestamp()
            WHERE id = v_target_id;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'guide-assets apply did not update one Project';
            END IF;

            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action, '{}'::jsonb
            );
        END IF;
    ELSIF p_action IN (
        'guide_account_exists', 'guide_read_project', 'guide_actor_status',
        'guide_read_member', 'guide_shared', 'guide_member', 'guide_lock',
        'guide_apply', 'guide_refresh'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'guide-assets action does not match operation';
    END IF;

    v_request_hash := pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(p_request::text, 'UTF8')),
        'hex'
    );
    v_txid := pg_catalog.txid_current();

    IF p_action = 'begin' THEN
        SELECT state.*
        INTO v_state
        FROM public.workspace_configuration_operation_states AS state
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        v_state_found := FOUND;
        IF v_state_found THEN
            IF v_state.request_hash IS DISTINCT FROM v_request_hash
               OR v_state.request IS DISTINCT FROM p_request THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 409,
                    pg_catalog.jsonb_build_object('reason', 'operation_conflict')
                );
            END IF;
            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action, v_state.facts
            );
        END IF;

        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'aap-workspace-configuration/v1:' || v_operation_id::text, 0
            )
        ) THEN
            RETURN aap_workspace_configuration_v1._step_failure(
                p_request, p_action, 409,
                pg_catalog.jsonb_build_object('reason', 'operation_busy')
            );
        END IF;

        SELECT receipt.*
        INTO v_receipt
        FROM public.workspace_configuration_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.target_id IS DISTINCT FROM v_target_id
               OR v_receipt.request_hash IS DISTINCT FROM v_request_hash
               OR v_receipt.selectors IS DISTINCT FROM v_selectors THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 409,
                    pg_catalog.jsonb_build_object('reason', 'operation_conflict')
                );
            END IF;
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;

            IF v_operation = 'delete_template' THEN
                v_frozen_template := pg_catalog.jsonb_build_object(
                    'scope', v_receipt.frozen_scope,
                    'organization_id', v_receipt.frozen_organization_id,
                    'created_by', v_receipt.frozen_creator_id
                );
                v_frozen_visible := aap_workspace_configuration_v1._configuration_template_visible(
                    v_actor, v_frozen_template
                );
                v_frozen_editable := v_actor->>'role' = 'super_admin'
                    OR v_receipt.frozen_creator_id = v_actor_id;
                IF NOT v_frozen_visible THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                IF NOT v_frozen_editable THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('仅模板创建者或超级管理员可删除'::text)
                    );
                END IF;
            ELSIF v_operation IN ('create_project', 'update_project') THEN
                v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    (v_receipt.result->>'resource_id')::uuid,
                    'key_share'
                );
                IF v_project IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                END IF;
                v_project_access := aap_workspace_configuration_v1._configuration_project_access(
                    v_actor, v_project, true
                );
                IF v_project_access->>'code' = 'project_missing' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                ELSIF v_project_access->>'code' = 'invalid_account_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                    );
                ELSIF v_project_access->>'code' = 'invalid_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                    );
                ELSIF v_project_access->>'code' = 'incompatible_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                    );
                ELSIF COALESCE((v_project_access->>'is_manager')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行'::text)
                    );
                END IF;
            ELSE
                v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                    (v_receipt.result->>'resource_id')::uuid,
                    true
                );
                IF v_template IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                IF NOT aap_workspace_configuration_v1._configuration_template_visible(
                    v_actor, v_template
                ) THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                IF v_actor->>'role' <> 'super_admin'
                   AND v_template->>'created_by' IS DISTINCT FROM v_actor_id::text THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('仅模板创建者或超级管理员可编辑'::text)
                    );
                END IF;
            END IF;
            v_facts := pg_catalog.jsonb_build_object(
                'actor', v_actor,
                'project', v_project,
                'source_project', NULL,
                'template', v_template,
                'replay', v_receipt.result,
                'project_access', v_project_access,
                'source_project_access', NULL,
                'is_manager', CASE
                    WHEN v_project_access IS NULL THEN NULL
                    ELSE v_project_access->'is_manager'
                END
            );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action, v_facts
            );
        END IF;

        IF v_operation = 'create_project' THEN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, false, NULL
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('无效的认证凭据'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            IF v_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('需要角色权限: super_admin或project_admin'::text)
                );
            END IF;
            IF v_selectors->>'source_project_id' IS NOT NULL THEN
                v_source_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    (v_selectors->>'source_project_id')::uuid, 'none'
                );
                IF v_source_project IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                END IF;
                v_source_project_access := aap_workspace_configuration_v1._configuration_project_access(
                    v_actor, v_source_project, false
                );
                IF v_source_project_access->>'code' = 'project_missing' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                ELSIF v_source_project_access->>'code' = 'invalid_account_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                    );
                ELSIF v_source_project_access->>'code' = 'invalid_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                    );
                ELSIF v_source_project_access->>'code' = 'incompatible_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                    );
                END IF;
            END IF;
            IF v_selectors->>'template_id' IS NOT NULL THEN
                v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                    (v_selectors->>'template_id')::uuid, false
                );
                IF v_template IS NULL OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                    v_actor, v_template
                ) THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
            END IF;
            v_is_creator := true;
            v_authorized := false;
        ELSIF v_operation = 'update_project' THEN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, false, NULL
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('无效的认证凭据'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                v_target_id, 'none'
            );
            IF v_project IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;
            v_project_access := aap_workspace_configuration_v1._configuration_project_access(
                v_actor, v_project, false
            );
            IF v_project_access->>'code' = 'project_missing' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            ELSIF v_project_access->>'code' = 'invalid_account_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                );
            ELSIF v_project_access->>'code' = 'invalid_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                );
            ELSIF v_project_access->>'code' = 'incompatible_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                );
            ELSIF COALESCE((v_project_access->>'is_manager')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行'::text)
                );
            END IF;
            v_is_creator := true;
            v_authorized := false;
        ELSIF v_operation = 'create_template' THEN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, false, NULL
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('无效的认证凭据'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            IF v_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目管理员或超级管理员可创建模板'::text)
                );
            END IF;
            v_scope := CASE
                WHEN (v_selectors->>'scope_present')::boolean
                THEN v_selectors->>'scope'
                ELSE 'private'
            END;
            v_organization_id := CASE
                WHEN pg_catalog.jsonb_typeof(v_selectors->'organization_id') = 'string'
                THEN (v_selectors->>'organization_id')::uuid
                ELSE NULL
            END;
            IF v_scope = 'public' AND v_actor->>'role' <> 'super_admin' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅超级管理员可创建公共模板'::text)
                );
            END IF;
            IF v_scope = 'organization' AND v_organization_id IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 400,
                    pg_catalog.to_jsonb('organization scope 必须指定 organization_id'::text)
                );
            END IF;
            v_authorized := false;
            v_is_creator := true;
        ELSE
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            IF v_operation = 'duplicate_template'
               AND v_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目管理员或超级管理员可克隆模板'::text)
                );
            END IF;
            v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                v_target_id, false
            );
            IF v_template IS NULL OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                v_actor, v_template
            ) THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('模板不存在'::text)
                );
            END IF;
            IF v_operation IN ('update_template', 'delete_template')
               AND v_actor->>'role' <> 'super_admin'
               AND v_template->>'created_by' IS DISTINCT FROM v_actor_id::text THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb(
                        CASE WHEN v_operation = 'delete_template'
                            THEN '仅模板创建者或超级管理员可删除'
                            ELSE '仅模板创建者或超级管理员可编辑'
                        END::text
                    )
                );
            END IF;
            v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                v_target_id, true
            );
            IF v_template IS NULL OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                v_actor, v_template
            ) THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('模板不存在'::text)
                );
            END IF;
            IF v_operation IN ('update_template', 'delete_template')
               AND v_actor->>'role' <> 'super_admin'
               AND v_template->>'created_by' IS DISTINCT FROM v_actor_id::text THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb(
                        CASE WHEN v_operation = 'delete_template'
                            THEN '仅模板创建者或超级管理员可删除'
                            ELSE '仅模板创建者或超级管理员可编辑'
                        END::text
                    )
                );
            END IF;
            v_authorized := true;
            v_is_creator := true;
        END IF;

        v_facts := pg_catalog.jsonb_build_object(
            'actor', v_actor,
            'project', v_project,
            'source_project', v_source_project,
            'template', v_template,
            'replay', NULL,
            'project_access', v_project_access,
            'source_project_access', v_source_project_access,
            'is_manager', COALESCE(v_project_access->'is_manager',
                CASE WHEN v_operation IN ('create_project', 'create_template')
                     OR v_operation = 'duplicate_template' THEN 'true'::jsonb
                     ELSE NULL END)
        );
        INSERT INTO public.workspace_configuration_operation_states (
            transaction_id, operation_id, request_hash, request, facts,
            authorized, source_project_id, target_id, source_backend_id,
            template_id
        ) VALUES (
            v_txid, v_operation_id, v_request_hash, p_request, v_facts,
            v_authorized,
            CASE WHEN v_selectors->>'source_project_id' IS NOT NULL
                 THEN (v_selectors->>'source_project_id')::uuid ELSE NULL END,
            v_target_id,
            CASE WHEN v_selectors->>'ml_backend_source_id' IS NOT NULL
                 THEN (v_selectors->>'ml_backend_source_id')::uuid ELSE NULL END,
            CASE
                WHEN v_operation IN ('update_template', 'delete_template', 'duplicate_template')
                    THEN v_target_id
                WHEN v_selectors->>'template_id' IS NOT NULL
                    THEN (v_selectors->>'template_id')::uuid
                ELSE NULL
            END
        );
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_facts
        );
    END IF;

    SELECT state.*
    INTO v_state
    FROM public.workspace_configuration_operation_states AS state
    WHERE state.transaction_id = v_txid
      AND state.operation_id = v_operation_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Workspace configuration operation has not begun';
    END IF;
    IF v_state.request_hash IS DISTINCT FROM v_request_hash
       OR v_state.request IS DISTINCT FROM p_request THEN
        RETURN aap_workspace_configuration_v1._step_failure(
            p_request, p_action, 409,
            pg_catalog.jsonb_build_object('reason', 'operation_conflict')
        );
    END IF;

    v_actor := NULLIF(v_state.facts->'actor', 'null'::jsonb);
    v_project := NULLIF(v_state.facts->'project', 'null'::jsonb);
    v_source_project := NULLIF(v_state.facts->'source_project', 'null'::jsonb);
    v_template := NULLIF(v_state.facts->'template', 'null'::jsonb);
    v_authorized := v_state.authorized;
    v_selectors := p_request->'selectors';

    IF p_action = 'authority' THEN
        IF v_operation NOT IN ('create_project', 'update_project', 'create_template') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'authority not allowed for operation';
        END IF;
        v_autoflush := (p_args->>'autoflush')::boolean;
        IF v_operation = 'create_project'
           AND v_state.source_project_id IS NULL
           AND v_state.binding_pending
           AND v_autoflush IS TRUE THEN
            IF v_state.resource_id IS NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Workspace project binding has no inserted project';
            END IF;
            SELECT project.ml_backend_pool_id
            INTO v_current_pool_id
            FROM public.projects AS project
            WHERE project.id = v_state.resource_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Workspace project binding target disappeared';
            END IF;
            IF v_current_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                INSERT INTO public.workspace_configuration_operation_attempts (
                    transaction_id, operation_id, request_hash
                ) VALUES (v_txid, v_operation_id, v_request_hash)
                ON CONFLICT (transaction_id, operation_id) DO NOTHING;
                UPDATE public.projects AS project
                SET ml_backend_pool_id = v_state.primary_pool_id,
                    updated_at = pg_catalog.now()
                WHERE project.id = v_state.resource_id;
                GET DIAGNOSTICS v_rows = ROW_COUNT;
                IF v_rows <> 1 THEN
                    RAISE EXCEPTION USING ERRCODE = 'XX000',
                        MESSAGE = 'Workspace primary backend flush did not update one project';
                END IF;
            END IF;
            UPDATE public.workspace_configuration_operation_states AS state
            SET binding_pending = false
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
            v_state.binding_pending := false;
        END IF;

        v_authority_actor := NULL;
        v_authority_project := NULL;
        v_authority_source_project := NULL;
        v_authority_template := NULL;
        v_authority_project_access := NULL;
        v_authority_source_access := NULL;

        IF v_operation = 'create_project' THEN
            IF v_state.source_project_id IS NOT NULL THEN
                v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                    v_actor_id, true, 'account_busy'
                );
                IF v_authority_actor IS NULL
                   OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 401,
                        pg_catalog.to_jsonb('账号已停用或不存在'::text),
                        pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                    );
                END IF;
                v_authority_source_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    v_state.source_project_id, 'key_share'
                );
                IF v_authority_source_project IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                END IF;
                v_authority_source_access := aap_workspace_configuration_v1._configuration_project_access(
                    v_authority_actor, v_authority_source_project, true
                );
                IF v_authority_source_access->>'code' = 'project_missing' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'invalid_account_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'invalid_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'incompatible_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                    );
                END IF;
                IF v_authority_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('需要角色权限: super_admin或project_admin'::text)
                    );
                END IF;
            ELSE
                v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                    v_actor_id, true, 'account_busy'
                );
                IF v_authority_actor IS NULL
                   OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 401,
                        pg_catalog.to_jsonb('账号已停用或不存在'::text),
                        pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                    );
                END IF;
                IF v_authority_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('需要角色权限: super_admin或project_admin'::text)
                    );
                END IF;
            END IF;
            IF v_selectors->>'template_id' IS NOT NULL THEN
                v_authority_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                    (v_selectors->>'template_id')::uuid, false
                );
                IF v_authority_template IS NULL
                   OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                       v_authority_actor, v_authority_template
                   ) THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                v_authority_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                    (v_selectors->>'template_id')::uuid, true
                );
                IF v_authority_template IS NULL
                   OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                       v_authority_actor, v_authority_template
                   ) THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
            END IF;
            IF v_state.resource_id IS NOT NULL THEN
                v_authority_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    v_state.resource_id, 'none'
                );
            END IF;
        ELSIF v_operation = 'create_template' THEN
            IF v_state.source_project_id IS NOT NULL THEN
                v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                    v_actor_id, true, 'account_busy'
                );
                IF v_authority_actor IS NULL
                   OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 401,
                        pg_catalog.to_jsonb('账号已停用或不存在'::text),
                        pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                    );
                END IF;
                v_authority_source_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    v_state.source_project_id, 'key_share'
                );
                IF v_authority_source_project IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                END IF;
                v_authority_source_access := aap_workspace_configuration_v1._configuration_project_access(
                    v_authority_actor, v_authority_source_project, true
                );
                IF v_authority_source_access->>'code' = 'project_missing' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'invalid_account_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'invalid_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'incompatible_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                    );
                END IF;
            ELSE
                v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                    v_actor_id, true, 'account_busy'
                );
                IF v_authority_actor IS NULL
                   OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 401,
                        pg_catalog.to_jsonb('账号已停用或不存在'::text),
                        pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                    );
                END IF;
            END IF;
            IF v_authority_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目管理员或超级管理员可创建模板'::text)
                );
            END IF;
            v_scope := CASE
                WHEN (v_selectors->>'scope_present')::boolean
                THEN v_selectors->>'scope'
                ELSE 'private'
            END;
            IF v_scope = 'public' AND v_authority_actor->>'role' <> 'super_admin' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅超级管理员可创建公共模板'::text)
                );
            END IF;
        ELSE
            v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_authority_actor IS NULL
               OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            v_authority_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                v_target_id, 'key_share'
            );
            IF v_authority_project IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;
            v_authority_project_access := aap_workspace_configuration_v1._configuration_project_access(
                v_authority_actor, v_authority_project, true
            );
            IF v_authority_project_access->>'code' = 'project_missing' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            ELSIF v_authority_project_access->>'code' = 'invalid_account_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                );
            ELSIF v_authority_project_access->>'code' = 'invalid_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                );
            ELSIF v_authority_project_access->>'code' = 'incompatible_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                );
            ELSIF COALESCE((v_authority_project_access->>'is_manager')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行'::text)
                );
            END IF;
        END IF;

        v_facts := pg_catalog.jsonb_build_object(
            'actor', v_authority_actor,
            'project', v_authority_project,
            'source_project', v_authority_source_project,
            'template', v_authority_template,
            'replay', NULL,
            'project_access', v_authority_project_access,
            'source_project_access', v_authority_source_access,
            'is_manager', COALESCE(
                v_authority_project_access->'is_manager',
                CASE WHEN v_operation IN ('create_project', 'create_template')
                     THEN 'true'::jsonb ELSE NULL END
            )
        );
        UPDATE public.workspace_configuration_operation_states AS state
        SET facts = v_facts,
            authorized = true,
            completed_actions = CASE
                WHEN 'authority' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'authority')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_facts
        );
    ELSIF p_action = 'allocate' THEN
        IF v_operation NOT IN ('create_project', 'create_template', 'duplicate_template') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'allocation not allowed';
        END IF;
        IF NOT v_authorized AND NOT (
            v_operation = 'create_project'
            AND v_state.source_backend_id IS NOT NULL
            AND v_state.source_backend_ready
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace configuration allocation requires authority';
        END IF;
        IF v_state.resource_id IS NOT NULL THEN
            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action,
                pg_catalog.jsonb_build_object(
                    'resource_id', v_state.resource_id,
                    'display_id', v_state.display_id
                )
            );
        END IF;
        v_resource_id := pg_catalog.gen_random_uuid();
        IF v_operation = 'create_project' THEN
            v_display_id := 'P-' || pg_catalog.nextval(
                'public.display_seq_projects'::pg_catalog.regclass
            )::text;
        ELSE
            v_display_id := 'PT-' || pg_catalog.nextval(
                'public.display_seq_project_templates'::pg_catalog.regclass
            )::text;
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET resource_id = v_resource_id,
            display_id = v_display_id,
            completed_actions = CASE
                WHEN 'allocate' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'allocate')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object(
                'resource_id', v_resource_id,
                'display_id', v_display_id
            )
        );
    ELSIF p_action = 'insert' THEN
        IF v_operation NOT IN ('create_project', 'create_template', 'duplicate_template')
           OR v_state.resource_id IS NULL
           OR (
               NOT v_authorized
               AND NOT (
                   v_operation = 'create_project'
                   AND v_state.source_backend_id IS NOT NULL
                   AND v_state.source_backend_ready
               )
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace configuration insert is not admitted';
        END IF;
        INSERT INTO public.workspace_configuration_operation_attempts (
            transaction_id, operation_id, request_hash
        ) VALUES (v_txid, v_operation_id, v_request_hash)
        ON CONFLICT (transaction_id, operation_id) DO NOTHING;
        v_columns_sql := 'id, display_id';
        v_values_sql := pg_catalog.quote_nullable(v_state.resource_id::text) || '::uuid, '
            || pg_catalog.quote_nullable(v_state.display_id) || '::text';
        IF v_operation = 'create_project' THEN
            v_table := 'public.projects';
            v_columns_sql := v_columns_sql || ', owner_id';
            v_values_sql := v_values_sql || ', ' || pg_catalog.quote_nullable(v_actor_id::text) || '::uuid';
        ELSE
            v_table := 'public.project_templates';
            v_columns_sql := v_columns_sql || ', created_by, source_project_id';
            v_values_sql := v_values_sql || ', '
                || pg_catalog.quote_nullable(v_actor_id::text) || '::uuid, '
                || pg_catalog.quote_nullable(CASE
                    WHEN v_operation = 'duplicate_template'
                    THEN v_template->>'source_project_id'
                    WHEN v_state.source_project_id IS NOT NULL
                    THEN v_state.source_project_id::text
                    ELSE NULL
                END) || '::uuid';
        END IF;
        FOR v_index IN 1..COALESCE(v_column_count, 0) LOOP
            v_name := p_column_names[v_index];
            v_value := p_column_values[v_index];
            IF v_operation = 'create_project' THEN
                IF v_name NOT IN (
                    'name', 'type_label', 'type_key', 'data_type', 'tool_bindings',
                    'ai_enabled', 'label_config', 'sampling', 'maximum_annotations',
                    'show_overlap_first', 'iou_dedup_threshold', 'box_threshold',
                    'text_threshold', 'rendering_config', 'annotation_guide',
                    'guide_assets',
                    'due_date', 'ai_interactive_enabled',
                    'raster_mask_native_editing_enabled', 'video_sampling',
                    'video_collaboration', 'scene_mode', 'prefer_same_scene_continuation'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'project insert column is not allowed';
                END IF;
            ELSE
                IF v_name NOT IN (
                    'name', 'description', 'type_label', 'type_key', 'data_type',
                    'tool_bindings', 'label_config', 'ai_enabled', 'sampling',
                    'maximum_annotations', 'show_overlap_first', 'iou_dedup_threshold',
                    'box_threshold', 'text_threshold', 'rendering_config',
                    'annotation_guide', 'scope', 'organization_id'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'template insert column is not allowed';
                END IF;
            END IF;
            IF v_name IN (
                'tool_bindings', 'label_config', 'rendering_config',
                'video_sampling', 'video_collaboration', 'guide_assets',
                'default_variants', 'preannotate_pipeline', 'mask_qc_config',
                'point_cloud_quality_config'
            ) THEN
                v_cast := '::jsonb';
                IF v_value IS NULL THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'JSON configuration values must use JSON text';
                END IF;
            ELSIF v_name = 'due_date' THEN
                v_cast := '::date';
            ELSIF v_name = 'organization_id' THEN
                v_cast := '::uuid';
            ELSIF v_name IN ('ai_enabled', 'show_overlap_first', 'scene_mode',
                             'prefer_same_scene_continuation',
                             'ai_interactive_enabled',
                             'raster_mask_native_editing_enabled') THEN
                v_cast := '::boolean';
            ELSIF v_name = 'maximum_annotations' THEN
                v_cast := '::integer';
            ELSIF v_name IN ('iou_dedup_threshold', 'box_threshold', 'text_threshold') THEN
                IF v_operation = 'create_project' AND v_name IN ('box_threshold', 'text_threshold') THEN
                    v_cast := '::real';
                ELSE
                    v_cast := '::double precision';
                END IF;
            ELSE
                v_cast := '::text';
            END IF;
            v_columns_sql := v_columns_sql || ', ' || pg_catalog.quote_ident(v_name);
            v_values_sql := v_values_sql || ', '
                || pg_catalog.quote_nullable(v_value) || v_cast;
        END LOOP;
        IF v_operation IN ('create_template', 'duplicate_template')
           AND NOT ('name' = ANY(COALESCE(p_column_names, ARRAY[]::text[]))) THEN
            RAISE EXCEPTION USING ERRCODE = '23502',
                MESSAGE = 'null value in column "name" of relation "project_templates" violates not-null constraint',
                SCHEMA = 'public', TABLE = 'project_templates', COLUMN = 'name';
        END IF;
        v_sql := 'INSERT INTO ' || v_table || ' (' || v_columns_sql || ') VALUES ('
            || v_values_sql || ')';
        EXECUTE v_sql;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'insert' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'insert')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        IF v_operation = 'create_project' THEN
            v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                v_state.resource_id, 'none'
            );
            v_output := pg_catalog.jsonb_build_object(
                'resource_id', v_state.resource_id,
                'project', v_project
            );
        ELSE
            v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                v_state.resource_id, false
            );
            v_output := pg_catalog.jsonb_build_object(
                'resource_id', v_state.resource_id,
                'template', v_template
            );
        END IF;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_output
        );
    END IF;

    IF p_action = 'enable_backend' THEN
        IF v_operation NOT IN ('create_project', 'update_project') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'backend enablement not allowed';
        END IF;
        v_registry_id := (p_args->>'registry_id')::uuid;
        IF v_registry_id IS DISTINCT FROM NULLIF(v_selectors->>'ml_backend_id', '')::uuid
           AND v_registry_id IS DISTINCT FROM NULLIF(
               v_selectors->>'ml_backend_source_id', ''
           )::uuid
           AND v_registry_id IS DISTINCT FROM v_state.source_backend_id THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unscoped backend enablement';
        END IF;
        IF v_state.resource_id IS NULL AND v_operation = 'create_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'backend requires an inserted project';
        END IF;
        IF v_operation = 'create_project'
           AND NOT v_authorized
           AND NOT (
               v_state.source_backend_id = v_registry_id
               AND v_state.source_backend_ready
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'pre-authority backend enablement requires a checked source backend';
        END IF;
        v_project_id := CASE
            WHEN v_operation = 'create_project' THEN v_state.resource_id
            ELSE v_target_id
        END;
        v_backend_reply := aap_inference_v1.enable_workspace_project_backend(
            v_project_id, v_registry_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_backend_reply,
            ARRAY['schema', 'project_id', 'registry_id', 'pool_id']::text[]
        ) OR v_backend_reply->>'schema' IS DISTINCT FROM
            'aap-inference-workspace-configuration/v1'
          OR v_backend_reply->>'project_id' IS DISTINCT FROM v_project_id::text
          OR v_backend_reply->>'registry_id' IS DISTINCT FROM v_registry_id::text
          OR pg_catalog.jsonb_typeof(v_backend_reply->'pool_id') NOT IN ('null', 'string') THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Inference enable reply';
        END IF;
        v_enabled_pool_id := CASE
            WHEN pg_catalog.jsonb_typeof(v_backend_reply->'pool_id') = 'string'
            THEN (v_backend_reply->>'pool_id')::uuid
            ELSE NULL
        END;
        IF v_enabled_pool_id IS NOT NULL THEN
            INSERT INTO public.workspace_configuration_operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (v_txid, v_operation_id, v_request_hash)
            ON CONFLICT (transaction_id, operation_id) DO NOTHING;
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET primary_pool_id = v_enabled_pool_id,
            completed_actions = CASE
                WHEN 'enable_backend' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'enable_backend')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('pool_id', v_backend_reply->'pool_id')
        );
    ELSIF p_action = 'binding' THEN
        IF v_operation NOT IN ('create_project', 'update_project') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'primary binding not allowed';
        END IF;
        v_pool_id := CASE
            WHEN pg_catalog.jsonb_typeof(p_args->'pool_id') = 'string'
            THEN (p_args->>'pool_id')::uuid
            ELSE NULL
        END;
        IF v_operation = 'create_project' THEN
            IF v_state.resource_id IS NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'binding requires an inserted project';
            END IF;
            IF v_state.source_backend_id IS NOT NULL
               AND v_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'binding pool does not match enabled source backend';
            END IF;
            IF v_state.source_backend_id IS NULL
               AND v_selectors->>'ml_backend_id' IS NOT NULL
               AND 'enable_backend' = ANY(v_state.completed_actions)
               AND v_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'binding pool does not match enabled backend';
            END IF;
        ELSE
            IF (v_selectors->>'ml_backend_present')::boolean IS DISTINCT FROM true THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'primary binding was not requested';
            END IF;
            IF v_selectors->>'ml_backend_id' IS NULL AND v_pool_id IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'cleared primary binding must be null';
            END IF;
            IF v_selectors->>'ml_backend_id' IS NOT NULL
               AND 'enable_backend' = ANY(v_state.completed_actions)
               AND v_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'binding pool does not match enabled backend';
            END IF;
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET primary_pool_id = v_pool_id,
            binding_pending = true,
            completed_actions = CASE
                WHEN 'binding' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'binding')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, '{}'::jsonb
        );
    ELSIF p_action = 'update' THEN
        IF v_operation NOT IN ('update_project', 'update_template') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'configuration update not allowed';
        END IF;
        IF v_operation = 'update_project' AND NOT v_authorized THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project update lacks final authority';
        END IF;
        IF v_operation = 'update_template' AND NOT v_authorized THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'template update lacks authority';
        END IF;
        v_assignments_sql := '';
        FOR v_index IN 1..COALESCE(v_column_count, 0) LOOP
            v_name := p_column_names[v_index];
            v_value := p_column_values[v_index];
            IF v_operation = 'update_project' THEN
                IF v_name NOT IN (
                    'name', 'type_label', 'type_key', 'data_type', 'status',
                    'tool_bindings', 'ai_enabled', 'due_date', 'sampling',
                    'maximum_annotations', 'show_overlap_first',
                    'iou_dedup_threshold', 'box_threshold', 'text_threshold',
                    'ai_interactive_enabled', 'raster_mask_native_editing_enabled',
                    'mask_qc_config', 'point_cloud_quality_config',
                    'rendering_config', 'annotation_guide', 'guide_assets',
                    'video_sampling', 'video_collaboration', 'scene_mode',
                    'prefer_same_scene_continuation', 'scene_continuation_window_min',
                    'default_variants', 'preannotate_pipeline'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'project update column is not allowed';
                END IF;
            ELSE
                IF v_name NOT IN (
                    'name', 'description', 'type_label', 'type_key', 'data_type',
                    'tool_bindings', 'label_config', 'ai_enabled', 'sampling',
                    'maximum_annotations', 'show_overlap_first',
                    'iou_dedup_threshold', 'box_threshold', 'text_threshold',
                    'rendering_config', 'annotation_guide', 'scope', 'organization_id'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'template update column is not allowed';
                END IF;
            END IF;
            IF v_name IN (
                'tool_bindings', 'label_config', 'rendering_config',
                'video_sampling', 'video_collaboration', 'guide_assets',
                'default_variants', 'preannotate_pipeline', 'mask_qc_config',
                'point_cloud_quality_config'
            ) THEN
                v_cast := '::jsonb';
                IF v_value IS NULL THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'JSON configuration values must use JSON text';
                END IF;
            ELSIF v_name = 'due_date' THEN
                v_cast := '::date';
            ELSIF v_name = 'organization_id' THEN
                v_cast := '::uuid';
            ELSIF v_name IN (
                'ai_enabled', 'show_overlap_first', 'scene_mode',
                'prefer_same_scene_continuation', 'ai_interactive_enabled',
                'raster_mask_native_editing_enabled'
            ) THEN
                v_cast := '::boolean';
            ELSIF v_name IN ('maximum_annotations', 'scene_continuation_window_min') THEN
                v_cast := '::integer';
            ELSIF v_name IN ('iou_dedup_threshold', 'box_threshold', 'text_threshold') THEN
                IF v_operation = 'update_project'
                   AND v_name IN ('box_threshold', 'text_threshold') THEN
                    v_cast := '::real';
                ELSE
                    v_cast := '::double precision';
                END IF;
            ELSE
                v_cast := '::text';
            END IF;
            IF v_assignments_sql <> '' THEN
                v_assignments_sql := v_assignments_sql || ', ';
            END IF;
            v_assignments_sql := v_assignments_sql
                || pg_catalog.quote_ident(v_name) || ' = '
                || pg_catalog.quote_nullable(v_value) || v_cast;
        END LOOP;

        IF v_operation = 'update_project' AND v_state.binding_pending THEN
            SELECT project.ml_backend_pool_id
            INTO v_current_pool_id
            FROM public.projects AS project
            WHERE project.id = v_target_id;
            IF NOT FOUND THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;
            IF v_current_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                IF v_assignments_sql <> '' THEN
                    v_assignments_sql := v_assignments_sql || ', ';
                END IF;
                v_assignments_sql := v_assignments_sql
                    || 'ml_backend_pool_id = '
                    || pg_catalog.quote_nullable(v_state.primary_pool_id::text)
                    || '::uuid';
            END IF;
        END IF;

        IF v_assignments_sql <> '' THEN
            INSERT INTO public.workspace_configuration_operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (v_txid, v_operation_id, v_request_hash)
            ON CONFLICT (transaction_id, operation_id) DO NOTHING;
            v_table := CASE
                WHEN v_operation = 'update_project' THEN 'public.projects'
                ELSE 'public.project_templates'
            END;
            v_project_id := CASE
                WHEN v_operation = 'update_project' THEN v_target_id
                ELSE v_target_id
            END;
            v_sql := 'UPDATE ' || v_table || ' SET ' || v_assignments_sql
                || ', updated_at = pg_catalog.now() WHERE id = '
                || pg_catalog.quote_nullable(v_project_id::text) || '::uuid';
            EXECUTE v_sql;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                IF v_operation = 'update_template' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;
        END IF;
        IF v_operation = 'update_project' AND v_state.binding_pending THEN
            UPDATE public.workspace_configuration_operation_states AS state
            SET binding_pending = false
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'update' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'update')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        IF v_operation = 'update_project' THEN
            v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                v_target_id, 'none'
            );
            v_output := pg_catalog.jsonb_build_object(
                'resource_id', v_target_id,
                'project', v_project
            );
        ELSE
            v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                v_target_id, false
            );
            v_output := pg_catalog.jsonb_build_object(
                'resource_id', v_target_id,
                'template', v_template
            );
        END IF;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_output
        );
    ELSIF p_action = 'delete' THEN
        IF v_operation <> 'delete_template' OR NOT v_authorized THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'template deletion is not admitted';
        END IF;
        INSERT INTO public.workspace_configuration_operation_attempts (
            transaction_id, operation_id, request_hash
        ) VALUES (v_txid, v_operation_id, v_request_hash)
        ON CONFLICT (transaction_id, operation_id) DO NOTHING;
        DELETE FROM public.project_templates AS template
        WHERE template.id = v_target_id;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        IF v_rows <> 1 THEN
            RETURN aap_workspace_configuration_v1._step_failure(
                p_request, p_action, 404,
                pg_catalog.to_jsonb('模板不存在'::text)
            );
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'delete' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'delete')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('resource_id', v_target_id)
        );
    ELSIF p_action = 'finish' THEN
        IF (
            v_operation = 'create_project'
            AND (NOT v_authorized OR NOT ('insert' = ANY(v_state.completed_actions)))
        ) OR (
            v_operation IN ('create_template', 'duplicate_template')
            AND (NOT v_authorized OR NOT ('insert' = ANY(v_state.completed_actions)))
        ) OR (
            v_operation IN ('update_project', 'update_template')
            AND NOT ('update' = ANY(v_state.completed_actions))
        ) OR (
            v_operation = 'delete_template'
            AND NOT ('delete' = ANY(v_state.completed_actions))
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace configuration operation is incomplete';
        END IF;

        IF v_operation IN ('create_project', 'update_project')
           AND v_state.binding_pending THEN
            v_project_id := CASE
                WHEN v_operation = 'create_project' THEN v_state.resource_id
                ELSE v_target_id
            END;
            SELECT project.ml_backend_pool_id
            INTO v_current_pool_id
            FROM public.projects AS project
            WHERE project.id = v_project_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Workspace configuration binding target disappeared';
            END IF;
            IF v_current_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                INSERT INTO public.workspace_configuration_operation_attempts (
                    transaction_id, operation_id, request_hash
                ) VALUES (v_txid, v_operation_id, v_request_hash)
                ON CONFLICT (transaction_id, operation_id) DO NOTHING;
                UPDATE public.projects AS project
                SET ml_backend_pool_id = v_state.primary_pool_id,
                    updated_at = pg_catalog.now()
                WHERE project.id = v_project_id;
                GET DIAGNOSTICS v_rows = ROW_COUNT;
                IF v_rows <> 1 THEN
                    RAISE EXCEPTION USING ERRCODE = 'XX000',
                        MESSAGE = 'Workspace configuration binding did not update one project';
                END IF;
            END IF;
        END IF;

        IF v_operation = 'create_project'
           AND v_state.template_id IS NOT NULL THEN
            INSERT INTO public.workspace_configuration_operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (v_txid, v_operation_id, v_request_hash)
            ON CONFLICT (transaction_id, operation_id) DO NOTHING;
            UPDATE public.project_templates AS template
            SET usage_count = template.usage_count + 1,
                updated_at = pg_catalog.now()
            WHERE template.id = v_state.template_id;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Workspace template usage increment did not update one template';
            END IF;
        END IF;

        v_resource_id := CASE
            WHEN v_operation IN ('create_project', 'create_template', 'duplicate_template')
            THEN v_state.resource_id
            ELSE v_target_id
        END;
        v_id := CASE
            WHEN v_operation = 'create_project'
            THEN v_state.template_id
            ELSE NULL
        END;
        v_result := pg_catalog.jsonb_build_object(
            'resource_id', v_resource_id,
            'template_id', v_id
        );
        IF pg_catalog.jsonb_typeof(v_result) IS DISTINCT FROM 'object'
           OR v_result->>'resource_id' IS DISTINCT FROM v_resource_id::text
           OR (
               v_id IS NULL AND v_result->'template_id' IS DISTINCT FROM 'null'::jsonb
           ) OR (
               v_id IS NOT NULL AND v_result->>'template_id' IS DISTINCT FROM v_id::text
           ) THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration finish result';
        END IF;
        INSERT INTO public.workspace_configuration_operation_receipts (
            operation_id, actor_id, operation, target_id, request_hash,
            selectors, result, frozen_scope, frozen_organization_id,
            frozen_creator_id
        ) VALUES (
            v_operation_id, v_actor_id, v_operation, v_target_id,
            v_request_hash, v_selectors, v_result,
            CASE WHEN v_operation = 'delete_template' THEN v_template->>'scope' ELSE NULL END,
            CASE WHEN v_operation = 'delete_template'
                 AND pg_catalog.jsonb_typeof(v_template->'organization_id') = 'string'
                 THEN (v_template->>'organization_id')::uuid ELSE NULL END,
            CASE WHEN v_operation = 'delete_template'
                 AND pg_catalog.jsonb_typeof(v_template->'created_by') = 'string'
                 THEN (v_template->>'created_by')::uuid ELSE NULL END
        );
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        IF v_rows <> 1 THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Workspace configuration receipt was not recorded';
        END IF;
        DELETE FROM public.workspace_configuration_operation_attempts AS attempt
        WHERE attempt.transaction_id = v_txid
          AND attempt.operation_id = v_operation_id;
        DELETE FROM public.workspace_configuration_operation_states AS state
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_result
        );
    END IF;

    IF p_action = 'backend' THEN
        IF v_operation NOT IN ('create_project', 'update_project') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'backend lookup not allowed';
        END IF;
        v_registry_id := (p_args->>'registry_id')::uuid;
        IF v_registry_id IS DISTINCT FROM NULLIF(v_selectors->>'ml_backend_id', '')::uuid
           AND v_registry_id IS DISTINCT FROM NULLIF(
               v_selectors->>'ml_backend_source_id', ''
           )::uuid
           AND v_registry_id IS DISTINCT FROM v_state.source_backend_id THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unscoped backend lookup';
        END IF;
        v_backend_reply := aap_inference_v1.workspace_configuration_backend(
            v_registry_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_backend_reply, ARRAY['schema', 'registry_id', 'backend']::text[]
        ) OR v_backend_reply->>'schema' IS DISTINCT FROM
            'aap-inference-workspace-configuration/v1'
          OR v_backend_reply->>'registry_id' IS DISTINCT FROM v_registry_id::text
          OR pg_catalog.jsonb_typeof(v_backend_reply->'backend') NOT IN ('null', 'object') THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Inference backend reply';
        END IF;
        IF v_state.source_backend_id = v_registry_id
           AND pg_catalog.jsonb_typeof(v_backend_reply->'backend') = 'object' THEN
            UPDATE public.workspace_configuration_operation_states AS state
            SET source_backend_ready = true,
                completed_actions = CASE
                    WHEN 'backend' = ANY(state.completed_actions) THEN state.completed_actions
                    ELSE pg_catalog.array_append(state.completed_actions, 'backend')
                END
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
        ELSE
            UPDATE public.workspace_configuration_operation_states AS state
            SET completed_actions = CASE
                    WHEN 'backend' = ANY(state.completed_actions) THEN state.completed_actions
                    ELSE pg_catalog.array_append(state.completed_actions, 'backend')
                END
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
        END IF;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('backend', v_backend_reply->'backend')
        );
    ELSIF p_action = 'source_pool' THEN
        IF v_operation <> 'create_project'
           OR v_source_project IS NULL
           OR (v_source_project->>'ml_backend_pool_id') IS DISTINCT FROM p_args->>'pool_id' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unscoped source-pool lookup';
        END IF;
        v_pool_id := (p_args->>'pool_id')::uuid;
        v_support_reply := aap_inference_v1.workspace_configuration_source_pool(
            v_pool_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_support_reply,
            ARRAY['schema', 'pool_id', 'legacy_registry_id', 'found']::text[]
        ) OR v_support_reply->>'schema' IS DISTINCT FROM
            'aap-inference-workspace-configuration/v1'
          OR v_support_reply->>'pool_id' IS DISTINCT FROM v_pool_id::text
          OR pg_catalog.jsonb_typeof(v_support_reply->'found') IS DISTINCT FROM 'boolean'
          OR pg_catalog.jsonb_typeof(v_support_reply->'legacy_registry_id') NOT IN ('null', 'string') THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Inference source-pool reply';
        END IF;
        v_registry_id := CASE
            WHEN pg_catalog.jsonb_typeof(v_support_reply->'legacy_registry_id') = 'string'
            THEN (v_support_reply->>'legacy_registry_id')::uuid
            ELSE NULL
        END;
        UPDATE public.workspace_configuration_operation_states AS state
        SET source_backend_id = v_registry_id,
            source_backend_ready = false,
            completed_actions = CASE
                WHEN 'source_pool' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'source_pool')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object(
                'found', v_support_reply->'found',
                'legacy_registry_id', v_support_reply->'legacy_registry_id'
            )
        );
    ELSIF p_action IN ('video_annotations', 'video_trackers', 'video_items', 'video_media') THEN
        IF v_operation <> 'update_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'video facts require a project update';
        END IF;
        IF p_action = 'video_annotations' THEN
            v_support_reply := aap_labeling_v1.workspace_configuration_annotation_count(
                v_target_id
            );
            IF NOT aap_workspace_configuration_v1._has_exact_keys(
                v_support_reply, ARRAY['schema', 'project_id', 'annotation_count']::text[]
            ) OR v_support_reply->>'schema' IS DISTINCT FROM
                'aap-labeling-workspace-configuration/v1'
              OR v_support_reply->>'project_id' IS DISTINCT FROM v_target_id::text THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Workspace configuration annotation facts';
            END IF;
            v_output := pg_catalog.jsonb_build_object(
                'annotation_count', v_support_reply->'annotation_count'
            );
        ELSIF p_action = 'video_trackers' THEN
            v_support_reply := aap_labeling_v1.workspace_configuration_tracker_count(
                v_target_id
            );
            IF NOT aap_workspace_configuration_v1._has_exact_keys(
                v_support_reply, ARRAY['schema', 'project_id', 'active_tracker_count']::text[]
            ) OR v_support_reply->>'schema' IS DISTINCT FROM
                'aap-labeling-workspace-configuration/v1'
              OR v_support_reply->>'project_id' IS DISTINCT FROM v_target_id::text THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Workspace configuration tracker facts';
            END IF;
            v_output := pg_catalog.jsonb_build_object(
                'active_tracker_count', v_support_reply->'active_tracker_count'
            );
        ELSIF p_action = 'video_items' THEN
            v_support_reply := aap_labeling_v1.workspace_configuration_video_items(
                v_target_id
            );
            IF NOT aap_workspace_configuration_v1._has_exact_keys(
                v_support_reply, ARRAY['schema', 'project_id', 'dataset_item_ids']::text[]
            ) OR v_support_reply->>'schema' IS DISTINCT FROM
                'aap-labeling-workspace-configuration/v1'
              OR v_support_reply->>'project_id' IS DISTINCT FROM v_target_id::text
              OR pg_catalog.jsonb_typeof(v_support_reply->'dataset_item_ids') IS DISTINCT FROM 'array'
              OR EXISTS (
                  SELECT 1
                  FROM pg_catalog.jsonb_array_elements(v_support_reply->'dataset_item_ids') AS item(value)
                  WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                     OR item.value #>> '{}' !~* v_uuid_pattern
              ) THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Workspace configuration video item facts';
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg(DISTINCT (item.value #>> '{}')::uuid),
                ARRAY[]::uuid[]
            )
            INTO v_media_ids
            FROM pg_catalog.jsonb_array_elements(v_support_reply->'dataset_item_ids') AS item(value);
            UPDATE public.workspace_configuration_operation_states AS state
            SET media_item_ids = v_media_ids,
                completed_actions = CASE
                    WHEN 'video_items' = ANY(state.completed_actions) THEN state.completed_actions
                    ELSE pg_catalog.array_append(state.completed_actions, 'video_items')
                END
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
            v_output := pg_catalog.jsonb_build_object(
                'dataset_item_ids', v_support_reply->'dataset_item_ids'
            );
        ELSE
            SELECT COALESCE(
                pg_catalog.array_agg(item.value::uuid ORDER BY item.ordinality),
                ARRAY[]::uuid[]
            )
            INTO v_item_ids
            FROM pg_catalog.jsonb_array_elements_text(p_args->'item_ids')
                WITH ORDINALITY AS item(value, ordinality);
            IF NOT v_state.media_item_ids @> v_item_ids THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'video media ids were not read from the selected project';
            END IF;
            v_media_reply := aap_assets_v1.read_media_summaries(v_item_ids);
            IF v_media_reply->>'schema' IS DISTINCT FROM 'aap-assets-media/v1'
               OR v_media_reply->'ok' IS DISTINCT FROM 'true'::jsonb
               OR pg_catalog.jsonb_typeof(v_media_reply->'data') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_media_reply #> '{data,items}') IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Workspace configuration Assets media reply';
            END IF;
            v_summaries := '{}'::jsonb;
            FOR v_summary IN
                SELECT item.value
                FROM pg_catalog.jsonb_array_elements(v_media_reply #> '{data,items}') AS item(value)
            LOOP
                IF pg_catalog.jsonb_typeof(v_summary->'dataset_item_id') IS DISTINCT FROM 'string'
                   OR (v_summary->>'dataset_item_id') !~* v_uuid_pattern
                   OR NOT v_item_ids @> ARRAY[(v_summary->>'dataset_item_id')::uuid]::uuid[] THEN
                    RAISE EXCEPTION USING ERRCODE = 'XX000',
                        MESSAGE = 'invalid Workspace configuration Assets media item';
                END IF;
                IF v_summary->>'file_type' = 'video' THEN
                    v_summaries := v_summaries || pg_catalog.jsonb_build_object(
                        v_summary->>'dataset_item_id',
                        CASE
                            WHEN v_summary->>'source_drifted' = 'false'
                             AND v_summary->'source_generation' = v_summary->'materialized_generation'
                            THEN v_summary->'metadata'
                            ELSE 'null'::jsonb
                        END
                    );
                END IF;
            END LOOP;
            v_output := pg_catalog.jsonb_build_object('summaries', v_summaries);
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN p_action = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, p_action)
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_output
        );
    ELSIF p_action = 'task_count' THEN
        IF v_operation <> 'update_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'task count requires a project update';
        END IF;
        v_support_reply := aap_labeling_v1.workspace_configuration_task_count(
            v_target_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_support_reply, ARRAY['schema', 'project_id', 'total_tasks']::text[]
        ) OR v_support_reply->>'schema' IS DISTINCT FROM
            'aap-labeling-workspace-configuration/v1'
          OR v_support_reply->>'project_id' IS DISTINCT FROM v_target_id::text THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration task-count facts';
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'task_count' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'task_count')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('total_tasks', v_support_reply->'total_tasks')
        );
    ELSIF p_action = 'datasets' THEN
        IF v_operation <> 'update_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'dataset read requires a project update';
        END IF;
        v_dataset_reply := aap_assets_v1.workspace_configuration_dataset_rows(
            v_target_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_dataset_reply, ARRAY['schema', 'project_id', 'datasets']::text[]
        ) OR v_dataset_reply->>'schema' IS DISTINCT FROM
            'aap-assets-workspace-configuration/v1'
          OR v_dataset_reply->>'project_id' IS DISTINCT FROM v_target_id::text
          OR pg_catalog.jsonb_typeof(v_dataset_reply->'datasets') IS DISTINCT FROM 'array'
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.jsonb_array_elements(v_dataset_reply->'datasets') AS item(value)
              WHERE NOT aap_workspace_configuration_v1._has_exact_keys(
                  item.value, ARRAY['id', 'display_id', 'data_type']::text[]
              ) OR item.value->>'id' !~* v_uuid_pattern
                OR pg_catalog.jsonb_typeof(item.value->'display_id') IS DISTINCT FROM 'string'
                OR pg_catalog.jsonb_typeof(item.value->'data_type') IS DISTINCT FROM 'string'
          ) THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Assets dataset reply';
        END IF;
        SELECT COALESCE(
            pg_catalog.array_agg((item.value->>'id')::uuid ORDER BY item.ordinality),
            ARRAY[]::uuid[]
        )
        INTO v_dataset_ids
        FROM pg_catalog.jsonb_array_elements(v_dataset_reply->'datasets')
            WITH ORDINALITY AS item(value, ordinality);
        UPDATE public.workspace_configuration_operation_states AS state
        SET dataset_ids = v_dataset_ids,
            completed_actions = CASE
                WHEN 'datasets' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'datasets')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('datasets', v_dataset_reply->'datasets')
        );
    ELSIF p_action = 'dataset_scenes' THEN
        v_dataset_id := (p_args->>'dataset_id')::uuid;
        IF v_operation <> 'update_project'
           OR NOT v_state.dataset_ids @> ARRAY[v_dataset_id]::uuid[] THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unscoped dataset scene read';
        END IF;
        v_support_reply := aap_assets_v1.workspace_configuration_dataset_scenes(
            v_dataset_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_support_reply, ARRAY['schema', 'dataset_id', 'has_scenes']::text[]
        ) OR v_support_reply->>'schema' IS DISTINCT FROM
            'aap-assets-workspace-configuration/v1'
          OR v_support_reply->>'dataset_id' IS DISTINCT FROM v_dataset_id::text
          OR pg_catalog.jsonb_typeof(v_support_reply->'has_scenes') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Assets scene reply';
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'dataset_scenes' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'dataset_scenes')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('has_scenes', v_support_reply->'has_scenes')
        );
    ELSIF p_action = 'lock_config' THEN
        IF v_operation <> 'update_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project config lock requires update';
        END IF;
        BEGIN
            SELECT project.id INTO v_id
            FROM public.projects AS project
            WHERE project.id = v_target_id
            FOR UPDATE OF project NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE EXCEPTION USING
                ERRCODE = SQLSTATE,
                MESSAGE = SQLERRM,
                HINT = 'aap.workspace.configuration.project_busy';
        END;
        IF NOT FOUND THEN
            RETURN aap_workspace_configuration_v1._step_failure(
                p_request, p_action, 404,
                pg_catalog.to_jsonb('项目不存在'::text)
            );
        END IF;
        v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
            v_target_id, 'none'
        );
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'lock_config' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'lock_config')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object(
                'kind', p_args->'kind',
                'project', v_project
            )
        );
    END IF;

    RAISE EXCEPTION USING ERRCODE = '22023',
        MESSAGE = 'unhandled Workspace configuration action';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1.configuration_step(
    jsonb, text, jsonb, text[], text[]
) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_workspace_project_query_v1.project_query_step(
    p_request jsonb,
    p_action text,
    p_parameters jsonb,
    p_status text,
    p_search text,
    p_type_keys text[],
    p_data_types text[],
    p_member_id uuid,
    p_project_role text,
    p_created_from timestamptz,
    p_created_to timestamptz,
    p_offset bigint,
    p_limit integer
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_reply jsonb;
    v_member jsonb;
    v_assigned_at timestamptz;
    v_updated_at timestamptz;
    v_actor_id uuid;
    v_target_id uuid;
    v_normalized_request jsonb;
    v_facts jsonb;
    v_rows_json text;
BEGIN
    IF p_request->>'operation' IN (
        'get_project_access', 'require_project_manager', 'require_project_visible'
    ) THEN
        IF p_action IS NULL OR p_action NOT IN (
            'get_row', 'actor_status', 'access_member_rows'
        )
           OR NOT aap_workspace_project_query_v1._has_exact_keys(
               p_request,
               ARRAY[
                   'schema', 'request_id', 'operation', 'admitted_actor', 'target_id',
                   'filters_ascii', 'pagination', 'projection_evidence'
               ]::text[]
           )
           OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
           OR p_request->>'schema' IS DISTINCT FROM 'aap-workspace-project-query/v1'
           OR pg_catalog.jsonb_typeof(p_request->'request_id') IS DISTINCT FROM 'string'
           OR (p_request->>'request_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
           OR p_request->>'operation' NOT IN (
               'get_project_access', 'require_project_manager', 'require_project_visible'
           )
           OR NOT aap_workspace_project_query_v1._has_exact_keys(
               p_request->'admitted_actor', ARRAY['id', 'role']::text[]
           )
           OR pg_catalog.jsonb_typeof(p_request #> '{admitted_actor,id}') IS DISTINCT FROM 'string'
           OR (p_request #>> '{admitted_actor,id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request #> '{admitted_actor,role}') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request->'target_id') IS DISTINCT FROM 'string'
           OR (p_request->>'target_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request->'filters_ascii') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_request->'pagination') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_request->'projection_evidence') IS DISTINCT FROM 'null'
           OR p_parameters IS NOT NULL
           OR p_status IS NOT NULL OR p_search IS NOT NULL
           OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
           OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
           OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
           OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project access query step';
        END IF;

        v_actor_id := (p_request #>> '{admitted_actor,id}')::uuid;
        v_target_id := (p_request->>'target_id')::uuid;

        IF p_action IN ('get_row', 'actor_status') THEN
            v_normalized_request := pg_catalog.jsonb_set(
                p_request,
                '{operation}',
                pg_catalog.to_jsonb('get_project'::text),
                false
            );
            v_reply := aap_workspace_project_query_v1._project_query_step_impl(
                v_normalized_request,
                p_action,
                NULL::jsonb,
                NULL::text,
                NULL::text,
                NULL::text[],
                NULL::text[],
                NULL::uuid,
                NULL::text,
                NULL::timestamptz,
                NULL::timestamptz,
                NULL::bigint,
                NULL::integer
            );
            v_facts := v_reply->'facts';
        ELSE
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'id', membership.id,
                        'project_id', membership.project_id,
                        'user_id', membership.user_id,
                        'role', membership.role::text,
                        'assigned_by', membership.assigned_by,
                        'assigned_at', pg_catalog.to_jsonb(membership.assigned_at),
                        'version', membership.version,
                        'updated_at', pg_catalog.to_jsonb(membership.updated_at),
                        'weekly_target', membership.weekly_target
                    )
                ),
                '[]'::jsonb
            )::text
            INTO v_rows_json
            FROM public.project_members AS membership
            WHERE membership.project_id = v_target_id
              AND membership.user_id = v_actor_id;
            v_facts := pg_catalog.jsonb_build_object('rows_json', v_rows_json);
        END IF;

        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-project-query/v1',
            'request', p_request,
            'action', p_action,
            'parameters', p_parameters,
            'facts', v_facts
        );
    END IF;

    v_reply := aap_workspace_project_query_v1._project_query_step_impl(
        p_request,
        p_action,
        p_parameters,
        p_status,
        p_search,
        p_type_keys,
        p_data_types,
        p_member_id,
        p_project_role,
        p_created_from,
        p_created_to,
        p_offset,
        p_limit
    );

    v_member := v_reply #> '{facts,member}';
    IF p_action = 'member_access'
       AND pg_catalog.jsonb_typeof(v_member) = 'object' THEN
        -- Keep the legacy Python datetime failure boundary in this statement snapshot.
        SELECT membership.assigned_at, membership.updated_at
        INTO v_assigned_at, v_updated_at
        FROM public.project_members AS membership
        WHERE membership.id = (v_member->>'id')::uuid
          AND membership.project_id = (v_member->>'project_id')::uuid
          AND membership.user_id = (v_member->>'user_id')::uuid;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Project member authority row changed within a stable snapshot';
        END IF;

        IF (
            v_assigned_at IS NOT NULL
            AND pg_catalog.isfinite(v_assigned_at)
            AND (
                v_assigned_at < TIMESTAMPTZ '0001-01-01 00:00:00+00'
                OR v_assigned_at > TIMESTAMPTZ '9999-12-31 23:59:59.999999+00'
            )
        ) OR (
            v_updated_at IS NOT NULL
            AND pg_catalog.isfinite(v_updated_at)
            AND (
                v_updated_at < TIMESTAMPTZ '0001-01-01 00:00:00+00'
                OR v_updated_at > TIMESTAMPTZ '9999-12-31 23:59:59.999999+00'
            )
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22008',
                MESSAGE = 'Project member timestamp is outside the Python datetime range';
        END IF;
    END IF;

    RETURN v_reply;
END;
$function$;
