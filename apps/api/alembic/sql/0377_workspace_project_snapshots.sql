-- Carry the merged guide-assets revision through every Project snapshot.
--
-- Main's guide feature owns `projects.guide_assets` and advances
-- `projects.guide_assets_revision` atomically with the asset list. Workspace
-- Project snapshots must expose the current persisted revision so ORM
-- hydration, locked access, class rename, guide reads and list/get queries all
-- observe the same row. The frozen <= 0370 artifacts stay unchanged; this
-- artifact forward-replaces only the serializers that materialize a complete
-- Project row, plus the two read relations that expose Project fields.
--
-- `aap_workspace_project_query_v1._project_row_json` keeps its exact-key
-- admission boundary: every caller must now pass `guide_assets_revision`
-- alongside `guide_assets`. `aap_workspace_v1.project_facts` is extended
-- append-only because CREATE OR REPLACE VIEW preserves its owner and ACL; the
-- packaged `PROJECT_FACT_COLUMNS` records that native relation order.
-- `aap_workspace_v1.project_catalog_configs` gains the revision key inside its
-- existing `project_config` JSONB payload. No guide list, reference row or
-- upload row is read or written here: `guide_assets_step` remains the only
-- publication writer and callers keep their own transaction and locks.
--
-- Downgrade restores the frozen function bodies and the previous definitions.
-- Removing the appended view column requires DROP VIEW ... RESTRICT, so the
-- downgrade captures and restores the view owner and both table-level and
-- column-level ACLs before returning.

-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_workspace_project_query_v1._project_row_json(
    p_workspace_fields jsonb,
    p_total_tasks integer,
    p_completed_tasks integer,
    p_review_tasks integer,
    p_in_progress_tasks integer,
    p_batch_summary_text text
)
RETURNS text
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_workspace_fields IS NULL
       OR aap_workspace_project_query_v1._has_exact_keys(
        p_workspace_fields,
        ARRAY[
            'id', 'organization_id', 'display_id', 'name', 'type_label',
            'type_key', 'data_type', 'owner_id', 'status', 'ai_enabled',
            'ml_backend_pool_id', 'default_variants', 'preannotate_pipeline',
            'label_config', 'tool_bindings', 'sampling', 'maximum_annotations',
            'show_overlap_first', 'iou_dedup_threshold', 'box_threshold',
            'text_threshold', 'ai_interactive_enabled',
            'raster_mask_native_editing_enabled', 'mask_qc_config',
            'point_cloud_quality_config', 'rendering_config', 'annotation_guide',
            'guide_assets', 'guide_assets_revision', 'model_version',
            'task_lock_ttl_seconds',
            'video_sampling', 'video_collaboration', 'scene_mode',
            'prefer_same_scene_continuation', 'scene_continuation_window_min',
            'due_date', 'created_at', 'updated_at'
        ]::text[]
    ) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid fixed Workspace Project row fields';
    END IF;

    RETURN (
        p_workspace_fields || pg_catalog.jsonb_build_object(
            'total_tasks', p_total_tasks,
            'completed_tasks', p_completed_tasks,
            'review_tasks', p_review_tasks,
            'in_progress_tasks', p_in_progress_tasks,
            'batch_summary', p_batch_summary_text
        )
    )::text;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_project_query_v1._project_row_json(
        jsonb, integer, integer, integer, integer, text
    )
    FROM PUBLIC;
-- aap-statement-break
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
                    'guide_assets_revision', project.guide_assets_revision,
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
                    'guide_assets_revision', project.guide_assets_revision,
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
                    'guide_assets_revision', project.guide_assets_revision,
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
                'guide_assets_revision', project.guide_assets_revision,
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
CREATE OR REPLACE FUNCTION aap_workspace_project_query_v1._project_query_step_impl(
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
    v_operation text;
    v_actor_id uuid;
    v_actor_role text;
    v_target_id uuid;
    v_filter_text text;
    v_filters json;
    v_filter_value json;
    v_filter_type text;
    v_filter_text_value text;
    v_filter_type_keys text[];
    v_filter_data_types text[];
    v_filter_member_id uuid;
    v_filter_from_us numeric;
    v_filter_to_us numeric;
    v_key_count integer;
    v_key_distinct_count integer;
    v_only_expected_keys boolean;
    v_pagination jsonb;
    v_page numeric;
    v_page_size integer;
    v_expected_offset numeric;
    v_filters_apply boolean := false;
    v_limit integer;
    v_row_json text;
    v_rows_json text;
    v_facts jsonb;
    v_result jsonb;
    v_actor_status record;
    v_member record;
    v_receipt record;
    v_transfer_result_data jsonb;
    v_transfer_previous_owner_id uuid;
    v_transfer_updated_at timestamptz;
    v_transfer_changed boolean;
    v_evidence jsonb;
    v_evidence_source text;
    v_evidence_operation_id uuid;
    v_evidence_operation text;
    v_evidence_actor_id uuid;
    v_evidence_project_id uuid;
    v_evidence_owner_id uuid;
    v_evidence_role text;
    v_evidence_active boolean;
    v_evidence_new_owner_id uuid;
    v_receipt_found boolean;
    v_project_ids uuid[];
    v_dataset_ids uuid[];
    v_bucket_ends timestamptz[];
    v_ai_only boolean;
    v_count_text text;
BEGIN
    IF p_action IS NULL OR p_action NOT IN (
        'list_rows', 'count_rows', 'visible_rows', 'get_row', 'actor_status',
        'member_access', 'ai_counts', 'member_count', 'pool', 'my_roles',
        'annotation_count', 'task_series', 'annotation_series', 'dataset_ids',
        'dataset_item_count', 'linked_task_count', 'task_count', 'job_counts',
        'latest_job_status', 'batches', 'batch_task_counts', 'active_members',
        'projection_row', 'search_rows'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace Project query action';
    END IF;
    IF NOT aap_workspace_project_query_v1._has_exact_keys(
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
           'list_projects', 'query_projects', 'get_project', 'project_stats',
           'project_readiness', 'get_project_projection', 'search_projects'
       )
       OR NOT aap_workspace_project_query_v1._has_exact_keys(
           p_request->'admitted_actor', ARRAY['id', 'role']::text[]
       )
       OR pg_catalog.jsonb_typeof(p_request #> '{admitted_actor,id}') IS DISTINCT FROM 'string'
       OR (p_request #>> '{admitted_actor,id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request #> '{admitted_actor,role}') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'target_id') NOT IN ('null', 'string')
       OR (
           pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
           AND (p_request->>'target_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       )
       OR pg_catalog.jsonb_typeof(p_request->'filters_ascii') NOT IN ('null', 'string')
       OR pg_catalog.jsonb_typeof(p_request->'pagination') NOT IN ('null', 'object')
       OR pg_catalog.jsonb_typeof(p_request->'projection_evidence') NOT IN ('null', 'object') THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace Project query request';
    END IF;

    v_operation := p_request->>'operation';
    v_actor_id := (p_request #>> '{admitted_actor,id}')::uuid;
    v_actor_role := p_request #>> '{admitted_actor,role}';
    v_target_id := CASE
        WHEN pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
        THEN (p_request->>'target_id')::uuid
        ELSE NULL
    END;
    v_filter_text := p_request->>'filters_ascii';
    v_pagination := p_request->'pagination';
    v_evidence := p_request->'projection_evidence';

    IF NOT (
        (v_operation = 'list_projects'
            AND p_action IN ('list_rows', 'my_roles', 'ai_counts', 'member_count', 'pool'))
        OR (v_operation = 'query_projects'
            AND p_action IN ('count_rows', 'list_rows', 'my_roles', 'ai_counts', 'member_count', 'pool'))
        OR (v_operation = 'get_project'
            AND p_action IN ('get_row', 'actor_status', 'member_access', 'ai_counts', 'member_count', 'pool', 'my_roles'))
        OR (v_operation = 'project_stats'
            AND p_action IN ('visible_rows', 'annotation_count', 'task_series', 'annotation_series'))
        OR (v_operation = 'project_readiness'
            AND p_action IN ('get_row', 'actor_status', 'member_access', 'dataset_ids', 'dataset_item_count', 'linked_task_count', 'task_count', 'job_counts', 'latest_job_status', 'batches', 'batch_task_counts', 'active_members'))
        OR (v_operation = 'get_project_projection'
            AND p_action IN ('projection_row', 'ai_counts', 'member_count', 'pool'))
        OR (v_operation = 'search_projects' AND p_action = 'search_rows')
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Workspace Project query action does not match operation';
    END IF;

    v_filters_apply := p_action IN ('list_rows', 'count_rows');

    IF v_operation IN ('list_projects', 'query_projects', 'search_projects') THEN
        IF v_target_id IS NOT NULL
           OR pg_catalog.jsonb_typeof(p_request->'projection_evidence') <> 'null'
           OR v_filter_text IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project query filters';
        END IF;
        IF pg_catalog.octet_length(pg_catalog.convert_to(v_filter_text, 'UTF8'))
               <> pg_catalog.char_length(v_filter_text)
           OR EXISTS (
               SELECT 1
               FROM pg_catalog.generate_series(
                   1, pg_catalog.char_length(v_filter_text)
               ) AS position(index)
               WHERE pg_catalog.ascii(
                   pg_catalog.substr(v_filter_text, position.index, 1)
               ) < 32
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project query filters must be canonical ASCII';
        END IF;
    ELSE
        IF v_filter_text IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'unexpected Workspace Project query filters';
        END IF;
    END IF;

    IF v_operation IN ('get_project', 'project_readiness', 'get_project_projection') THEN
        IF v_target_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project query target is required';
        END IF;
    ELSIF v_target_id IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'unexpected Workspace Project query target';
    END IF;

    IF (v_operation = 'query_projects') <>
       (pg_catalog.jsonb_typeof(v_pagination) = 'object') THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Workspace Project pagination does not match operation';
    END IF;
    IF v_operation <> 'get_project_projection'
       AND pg_catalog.jsonb_typeof(v_evidence) <> 'null' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'unexpected Workspace Project projection evidence';
    END IF;

    IF v_operation = 'query_projects' THEN
        IF NOT aap_workspace_project_query_v1._has_exact_keys(
            v_pagination, ARRAY['page', 'page_size']::text[]
        )
           OR pg_catalog.jsonb_typeof(v_pagination->'page') IS DISTINCT FROM 'string'
           OR (v_pagination->>'page') !~ '^[1-9][0-9]*$'
           OR pg_catalog.jsonb_typeof(v_pagination->'page_size') IS DISTINCT FROM 'number'
           OR (v_pagination->>'page_size') !~ '^[1-9][0-9]{0,2}$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project pagination';
        END IF;
        BEGIN
            v_page := (v_pagination->>'page')::numeric;
            v_page_size := (v_pagination->>'page_size')::integer;
            IF v_page_size NOT BETWEEN 1 AND 100 THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project page size';
            END IF;
            v_expected_offset := (v_page - 1) * v_page_size;
        EXCEPTION WHEN numeric_value_out_of_range THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project page offset exceeds the numeric range';
        END;
    END IF;

    IF v_operation IN ('list_projects', 'query_projects') THEN
        BEGIN
            v_filters := v_filter_text::json;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project query filter JSON';
        END;
        IF pg_catalog.json_typeof(v_filters) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project query filters must be an object';
        END IF;
        SELECT pg_catalog.count(*)::integer,
               pg_catalog.count(DISTINCT field.key)::integer,
               pg_catalog.bool_and(field.key = ANY (
                   ARRAY[
                       'status', 'search', 'type_key', 'data_type', 'member_id',
                       'project_role', 'created_from', 'created_to'
                   ]::text[]
               ))
        INTO v_key_count, v_key_distinct_count, v_only_expected_keys
        FROM pg_catalog.json_each(v_filters) AS field(key, value);
        IF v_key_count <> 8 OR v_key_distinct_count <> 8
           OR v_only_expected_keys IS DISTINCT FROM true THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project filters require their exact eight fields';
        END IF;

        SELECT field.value INTO v_filter_value
        FROM pg_catalog.json_each(v_filters) AS field(key, value)
        WHERE field.key = 'status';
        v_filter_type := pg_catalog.json_typeof(v_filter_value);
        IF v_filter_type NOT IN ('null', 'string') THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project status filter';
        END IF;
        v_filter_text_value := pg_catalog.json_extract_path_text(v_filters, 'status');
        IF (v_filters_apply AND v_filter_text_value IS DISTINCT FROM p_status)
           OR (NOT v_filters_apply AND p_status IS NOT NULL) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project status filters disagree';
        END IF;

        SELECT field.value INTO v_filter_value
        FROM pg_catalog.json_each(v_filters) AS field(key, value)
        WHERE field.key = 'search';
        v_filter_type := pg_catalog.json_typeof(v_filter_value);
        IF v_filter_type NOT IN ('null', 'string') THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project search filter';
        END IF;
        v_filter_text_value := pg_catalog.json_extract_path_text(v_filters, 'search');
        IF (v_filters_apply AND v_filter_text_value IS DISTINCT FROM p_search)
           OR (NOT v_filters_apply AND p_search IS NOT NULL) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project search filters disagree';
        END IF;

        SELECT field.value INTO v_filter_value
        FROM pg_catalog.json_each(v_filters) AS field(key, value)
        WHERE field.key = 'type_key';
        v_filter_type := pg_catalog.json_typeof(v_filter_value);
        IF v_filter_type = 'null' THEN
            IF p_type_keys IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project type-key filters disagree';
            END IF;
            v_filter_type_keys := NULL;
        ELSIF v_filter_type = 'array' THEN
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.json_array_elements(v_filter_value) AS item(value)
                WHERE pg_catalog.json_typeof(item.value) IS DISTINCT FROM 'string'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project type-key list must contain strings';
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg(item.value ORDER BY item.ordinality),
                ARRAY[]::text[]
            )
            INTO v_filter_type_keys
            FROM pg_catalog.json_array_elements_text(v_filter_value)
                WITH ORDINALITY AS item(value, ordinality);
            IF (v_filters_apply AND (p_type_keys IS NULL OR v_filter_type_keys IS DISTINCT FROM p_type_keys))
               OR (NOT v_filters_apply AND p_type_keys IS NOT NULL) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project type-key filters disagree';
            END IF;
        ELSE
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project type-key filter';
        END IF;

        SELECT field.value INTO v_filter_value
        FROM pg_catalog.json_each(v_filters) AS field(key, value)
        WHERE field.key = 'data_type';
        v_filter_type := pg_catalog.json_typeof(v_filter_value);
        IF v_filter_type = 'null' THEN
            IF p_data_types IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project data-type filters disagree';
            END IF;
            v_filter_data_types := NULL;
        ELSIF v_filter_type = 'array' THEN
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.json_array_elements(v_filter_value) AS item(value)
                WHERE pg_catalog.json_typeof(item.value) IS DISTINCT FROM 'string'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project data-type list must contain strings';
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg(item.value ORDER BY item.ordinality),
                ARRAY[]::text[]
            )
            INTO v_filter_data_types
            FROM pg_catalog.json_array_elements_text(v_filter_value)
                WITH ORDINALITY AS item(value, ordinality);
            IF (v_filters_apply AND (p_data_types IS NULL OR v_filter_data_types IS DISTINCT FROM p_data_types))
               OR (NOT v_filters_apply AND p_data_types IS NOT NULL) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project data-type filters disagree';
            END IF;
        ELSE
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project data-type filter';
        END IF;

        SELECT field.value INTO v_filter_value
        FROM pg_catalog.json_each(v_filters) AS field(key, value)
        WHERE field.key = 'member_id';
        v_filter_type := pg_catalog.json_typeof(v_filter_value);
        IF v_filter_type = 'null' THEN
            IF p_member_id IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project member filters disagree';
            END IF;
            v_filter_member_id := NULL;
        ELSIF v_filter_type = 'string'
           AND v_filter_value #>> '{}' ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            v_filter_member_id := (v_filter_value #>> '{}')::uuid;
            IF (v_filters_apply AND p_member_id IS DISTINCT FROM v_filter_member_id)
               OR (NOT v_filters_apply AND p_member_id IS NOT NULL) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project member filters disagree';
            END IF;
        ELSE
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project member filter';
        END IF;

        SELECT field.value INTO v_filter_value
        FROM pg_catalog.json_each(v_filters) AS field(key, value)
        WHERE field.key = 'project_role';
        v_filter_type := pg_catalog.json_typeof(v_filter_value);
        IF v_filter_type NOT IN ('null', 'string') THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project role filter';
        END IF;
        v_filter_text_value := pg_catalog.json_extract_path_text(v_filters, 'project_role');
        IF (v_filters_apply AND v_filter_text_value IS DISTINCT FROM p_project_role)
           OR (NOT v_filters_apply AND p_project_role IS NOT NULL) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project role filters disagree';
        END IF;

        SELECT field.value INTO v_filter_value
        FROM pg_catalog.json_each(v_filters) AS field(key, value)
        WHERE field.key = 'created_from';
        v_filter_type := pg_catalog.json_typeof(v_filter_value);
        IF v_filter_type NOT IN ('null', 'string') THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project created-from filter';
        END IF;
        v_filter_text_value := pg_catalog.json_extract_path_text(v_filters, 'created_from');
        IF COALESCE(v_filter_text_value, '') = '' THEN
            IF p_created_from IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project created-from bind is unused';
            END IF;
        ELSIF v_filters_apply AND p_created_from IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project created-from bind is missing';
        ELSIF NOT v_filters_apply AND p_created_from IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project created-from bind is unused';
        END IF;

        SELECT field.value INTO v_filter_value
        FROM pg_catalog.json_each(v_filters) AS field(key, value)
        WHERE field.key = 'created_to';
        v_filter_type := pg_catalog.json_typeof(v_filter_value);
        IF v_filter_type NOT IN ('null', 'string') THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project created-to filter';
        END IF;
        v_filter_text_value := pg_catalog.json_extract_path_text(v_filters, 'created_to');
        IF COALESCE(v_filter_text_value, '') = '' THEN
            IF p_created_to IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project created-to bind is unused';
            END IF;
        ELSIF v_filters_apply AND p_created_to IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project created-to bind is missing';
        ELSIF NOT v_filters_apply AND p_created_to IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project created-to bind is unused';
        END IF;
    ELSIF v_operation = 'search_projects' THEN
        BEGIN
            v_filters := v_filter_text::json;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project search filter JSON';
        END;
        IF NOT aap_workspace_project_query_v1._has_exact_keys(
            pg_catalog.to_jsonb(v_filters), ARRAY['q', 'limit']::text[]
        )
           OR pg_catalog.json_typeof(v_filters) IS DISTINCT FROM 'object'
           OR pg_catalog.json_typeof(v_filters->'q') IS DISTINCT FROM 'string'
           OR pg_catalog.json_typeof(v_filters->'limit') IS DISTINCT FROM 'number'
           OR (v_filters->>'q') IS DISTINCT FROM p_search
           OR (v_filters->>'limit') !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.char_length(v_filters->>'limit') > 2
           OR (CASE
               WHEN (v_filters->>'limit') ~ '^(0|[1-9][0-9]{0,1})$'
               THEN (v_filters->>'limit')::integer
               ELSE NULL
              END) IS DISTINCT FROM p_limit
           OR p_search IS NULL OR pg_catalog.char_length(p_search) NOT BETWEEN 1 AND 64
           OR p_limit NOT BETWEEN 1 AND 20 THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project search filters';
        END IF;
    END IF;

    IF p_action IN ('list_rows', 'count_rows') THEN
        IF NOT aap_workspace_project_query_v1._has_exact_keys(
            p_parameters,
            ARRAY['created_from_us', 'created_to_us']::text[]
        )
           OR pg_catalog.jsonb_typeof(p_parameters->'created_from_us') NOT IN ('null', 'string')
           OR pg_catalog.jsonb_typeof(p_parameters->'created_to_us') NOT IN ('null', 'string')
           OR (
               pg_catalog.jsonb_typeof(p_parameters->'created_from_us') = 'string'
               AND (
                   pg_catalog.char_length(p_parameters->>'created_from_us') > 19
                   OR (p_parameters->>'created_from_us') !~ '^(0|-?[1-9][0-9]*)$'
               )
           )
           OR (
               pg_catalog.jsonb_typeof(p_parameters->'created_to_us') = 'string'
               AND (
                   pg_catalog.char_length(p_parameters->>'created_to_us') > 19
                   OR (p_parameters->>'created_to_us') !~ '^(0|-?[1-9][0-9]*)$'
               )
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project date-bound parameters';
        END IF;

        IF p_created_from IS NULL THEN
            IF pg_catalog.jsonb_typeof(p_parameters->'created_from_us') IS DISTINCT FROM 'null' THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project created-from bound does not match';
            END IF;
        ELSE
            IF pg_catalog.jsonb_typeof(p_parameters->'created_from_us') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project created-from bound does not match';
            END IF;
            v_filter_from_us := (p_parameters->>'created_from_us')::numeric;
            IF (EXTRACT(EPOCH FROM p_created_from) * 1000000)::numeric
               IS DISTINCT FROM v_filter_from_us THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project created-from bound does not match';
            END IF;
        END IF;

        IF p_created_to IS NULL THEN
            IF pg_catalog.jsonb_typeof(p_parameters->'created_to_us') IS DISTINCT FROM 'null' THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project created-to bound does not match';
            END IF;
        ELSE
            IF pg_catalog.jsonb_typeof(p_parameters->'created_to_us') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project created-to bound does not match';
            END IF;
            v_filter_to_us := (p_parameters->>'created_to_us')::numeric;
            IF (EXTRACT(EPOCH FROM p_created_to) * 1000000)::numeric
               IS DISTINCT FROM v_filter_to_us THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project created-to bound does not match';
            END IF;
        END IF;
    END IF;

    IF v_operation = 'query_projects' AND p_action = 'list_rows' THEN
        IF v_expected_offset > 9223372036854775807::numeric
           OR p_offset IS NULL
           OR p_limit IS DISTINCT FROM v_page_size
           OR p_offset::numeric IS DISTINCT FROM v_expected_offset THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project pagination binds disagree';
        END IF;
    ELSIF v_operation = 'query_projects'
       AND (p_offset IS NOT NULL OR p_limit IS NOT NULL) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'unexpected Workspace Project non-row pagination';
    ELSIF v_operation = 'list_projects' AND p_action = 'list_rows' THEN
        IF p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'unexpected Workspace Project list pagination';
        END IF;
    END IF;

    CASE p_action
        WHEN 'count_rows' THEN
            IF v_operation <> 'query_projects'
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project count_rows step';
            END IF;
            SELECT pg_catalog.count(*)::text
            INTO v_count_text
            FROM public.projects AS project
            WHERE (
                v_actor_role = 'super_admin'
                OR (v_actor_role = 'project_admin' AND project.owner_id = v_actor_id)
                OR EXISTS (
                    SELECT 1
                    FROM public.project_members AS membership
                    WHERE membership.project_id = project.id
                      AND membership.user_id = v_actor_id
                )
            )
              AND (COALESCE(p_status, '') = '' OR project.status = p_status)
              AND (COALESCE(p_search, '') = '' OR project.name ILIKE '%' || p_search || '%')
              AND (COALESCE(pg_catalog.cardinality(p_type_keys), 0) = 0
                   OR project.type_key = ANY(p_type_keys))
              AND (COALESCE(pg_catalog.cardinality(p_data_types), 0) = 0
                   OR project.data_type = ANY(p_data_types))
              AND (p_member_id IS NULL OR EXISTS (
                    SELECT 1
                    FROM public.project_members AS member_filter
                    WHERE member_filter.project_id = project.id
                      AND member_filter.user_id = p_member_id
              ))
              AND (COALESCE(p_project_role, '') = '' OR EXISTS (
                    SELECT 1
                    FROM public.project_members AS role_filter
                    WHERE role_filter.project_id = project.id
                      AND role_filter.user_id = v_actor_id
                      AND role_filter.role = p_project_role
              ))
              AND (p_created_from IS NULL OR project.created_at >= p_created_from)
              AND (p_created_to IS NULL OR project.created_at <= p_created_to);
            v_facts := pg_catalog.jsonb_build_object('count', v_count_text);

        WHEN 'actor_status' THEN
            IF v_operation NOT IN ('get_project', 'project_readiness')
               OR p_parameters IS NOT NULL
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project actor_status step';
            END IF;
            SELECT actor.actor_id, actor.platform_role, actor.is_active
            INTO v_actor_status
            FROM aap_identity_v1.read_actor_facts(v_actor_id) AS actor;
            IF FOUND THEN
                v_facts := pg_catalog.jsonb_build_object(
                    'actor', pg_catalog.jsonb_build_object(
                        'actor_id', v_actor_status.actor_id,
                        'platform_role', v_actor_status.platform_role,
                        'is_active', v_actor_status.is_active
                    )
                );
            ELSE
                v_facts := pg_catalog.jsonb_build_object('actor', NULL);
            END IF;

        WHEN 'member_access' THEN
            IF v_operation NOT IN ('get_project', 'project_readiness')
               OR p_parameters IS NOT NULL
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project member_access step';
            END IF;
            SELECT membership.id,
                   membership.role::text,
                   membership.project_id,
                   membership.user_id
            INTO v_member
            FROM public.project_members AS membership
            WHERE membership.project_id = v_target_id
              AND membership.user_id = v_actor_id;
            IF FOUND THEN
                v_facts := pg_catalog.jsonb_build_object(
                    'member', pg_catalog.jsonb_build_object(
                        'id', v_member.id,
                        'role', v_member.role,
                        'project_id', v_member.project_id,
                        'user_id', v_member.user_id
                    )
                );
            ELSE
                v_facts := pg_catalog.jsonb_build_object('member', NULL);
            END IF;

        WHEN 'ai_counts' THEN
            IF v_operation NOT IN (
                'list_projects', 'query_projects', 'get_project',
                'get_project_projection'
            )
               OR NOT aap_workspace_project_query_v1._has_exact_keys(
                   p_parameters, ARRAY['project_ids']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'project_ids') IS DISTINCT FROM 'array'
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project ai_counts step';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(p_parameters->'project_ids') AS item(value)
                WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                   OR (item.value #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project IDs';
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg((item.value #>> '{}')::uuid ORDER BY item.ordinality),
                ARRAY[]::uuid[]
            )
            INTO v_project_ids
            FROM pg_catalog.jsonb_array_elements(p_parameters->'project_ids')
                WITH ORDINALITY AS item(value, ordinality);
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'project_id', counts.project_id,
                        'count', counts.task_count::text
                    )
                ),
                '[]'::jsonb
            )
            INTO v_facts
            FROM aap_labeling_v1.workspace_project_query_ai_counts(v_project_ids)
                AS counts;
            v_facts := pg_catalog.jsonb_build_object('counts', v_facts);

        WHEN 'member_count' THEN
            IF v_operation NOT IN (
                'list_projects', 'query_projects', 'get_project',
                'get_project_projection'
            )
               OR NOT aap_workspace_project_query_v1._has_exact_keys(
                   p_parameters, ARRAY['project_id']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'project_id') IS DISTINCT FROM 'string'
               OR (p_parameters->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project member_count step';
            END IF;
            SELECT pg_catalog.count(*)::text
            INTO v_count_text
            FROM public.project_members AS membership
            WHERE membership.project_id = (p_parameters->>'project_id')::uuid;
            v_facts := pg_catalog.jsonb_build_object('count', v_count_text);

        WHEN 'pool' THEN
            IF v_operation NOT IN (
                'list_projects', 'query_projects', 'get_project',
                'get_project_projection'
            )
               OR NOT aap_workspace_project_query_v1._has_exact_keys(
                   p_parameters, ARRAY['pool_id']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'pool_id') IS DISTINCT FROM 'string'
               OR (p_parameters->>'pool_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project pool step';
            END IF;
            SELECT pg_catalog.jsonb_build_object(
                'legacy_instance_id', pool.legacy_instance_id
            )
            INTO v_facts
            FROM aap_inference_v1.workspace_project_query_pool(
                (p_parameters->>'pool_id')::uuid
            ) AS pool;
            IF NOT FOUND THEN
                v_facts := pg_catalog.jsonb_build_object('legacy_instance_id', NULL);
            END IF;

        WHEN 'my_roles' THEN
            IF v_operation NOT IN (
                'list_projects', 'query_projects', 'get_project',
                'get_project_projection'
            )
               OR NOT aap_workspace_project_query_v1._has_exact_keys(
                   p_parameters, ARRAY['project_ids']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'project_ids') IS DISTINCT FROM 'array'
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project my_roles step';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(p_parameters->'project_ids') AS item(value)
                WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                   OR (item.value #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project IDs';
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg((item.value #>> '{}')::uuid ORDER BY item.ordinality),
                ARRAY[]::uuid[]
            )
            INTO v_project_ids
            FROM pg_catalog.jsonb_array_elements(p_parameters->'project_ids')
                WITH ORDINALITY AS item(value, ordinality);
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'project_id', membership.project_id,
                        'role', membership.role::text
                    )
                ),
                '[]'::jsonb
            )
            INTO v_facts
            FROM public.project_members AS membership
            WHERE membership.user_id = v_actor_id
              AND membership.project_id = ANY(v_project_ids);
            v_facts := pg_catalog.jsonb_build_object('memberships', v_facts);

        WHEN 'annotation_count' THEN
            IF v_operation <> 'project_stats'
               OR NOT aap_workspace_project_query_v1._has_exact_keys(
                   p_parameters, ARRAY['project_ids', 'ai_only']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'project_ids') IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_typeof(p_parameters->'ai_only') IS DISTINCT FROM 'boolean'
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project annotation_count step';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(p_parameters->'project_ids') AS item(value)
                WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                   OR (item.value #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project IDs';
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg((item.value #>> '{}')::uuid ORDER BY item.ordinality),
                ARRAY[]::uuid[]
            )
            INTO v_project_ids
            FROM pg_catalog.jsonb_array_elements(p_parameters->'project_ids')
                WITH ORDINALITY AS item(value, ordinality);
            v_ai_only := (p_parameters->>'ai_only')::boolean;
            SELECT aap_labeling_v1.workspace_project_query_annotation_count(
                v_project_ids, v_ai_only
            )::text INTO v_count_text;
            v_facts := pg_catalog.jsonb_build_object('count', v_count_text);

        WHEN 'task_series' THEN
            IF v_operation <> 'project_stats'
               OR NOT aap_workspace_project_query_v1._has_exact_keys(
                   p_parameters, ARRAY['project_ids', 'bucket_ends']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'project_ids') IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_typeof(p_parameters->'bucket_ends') IS DISTINCT FROM 'array'
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project task_series step';
            END IF;
            IF pg_catalog.jsonb_array_length(p_parameters->'bucket_ends') <> 12 THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project task series requires twelve endpoints';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(p_parameters->'project_ids') AS item(value)
                WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                   OR (item.value #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            ) OR EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(p_parameters->'bucket_ends') AS item(value)
                WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project series parameters';
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg((item.value #>> '{}')::uuid ORDER BY item.ordinality),
                ARRAY[]::uuid[]
            )
            INTO v_project_ids
            FROM pg_catalog.jsonb_array_elements(p_parameters->'project_ids')
                WITH ORDINALITY AS item(value, ordinality);
            BEGIN
                SELECT pg_catalog.array_agg((item.value #>> '{}')::timestamptz ORDER BY item.ordinality)
                INTO v_bucket_ends
                FROM pg_catalog.jsonb_array_elements(p_parameters->'bucket_ends')
                    WITH ORDINALITY AS item(value, ordinality);
            EXCEPTION WHEN invalid_text_representation
                         OR invalid_datetime_format
                         OR datetime_field_overflow THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project series endpoints';
            END;
            SELECT COALESCE(
                pg_catalog.jsonb_build_object(
                    'total_data', pg_catalog.jsonb_agg(series.total_data::text ORDER BY series.bucket_index),
                    'completed', pg_catalog.jsonb_agg(series.completed::text ORDER BY series.bucket_index),
                    'pending_review', pg_catalog.jsonb_agg(series.pending_review::text ORDER BY series.bucket_index)
                ),
                pg_catalog.jsonb_build_object(
                    'total_data', '[]'::jsonb,
                    'completed', '[]'::jsonb,
                    'pending_review', '[]'::jsonb
                )
            )
            INTO v_facts
            FROM aap_labeling_v1.workspace_project_query_task_series(
                v_project_ids, v_bucket_ends
            ) AS series;

        WHEN 'annotation_series' THEN
            IF v_operation <> 'project_stats'
               OR NOT aap_workspace_project_query_v1._has_exact_keys(
                   p_parameters, ARRAY['project_ids', 'bucket_ends']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'project_ids') IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_typeof(p_parameters->'bucket_ends') IS DISTINCT FROM 'array'
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project annotation_series step';
            END IF;
            IF pg_catalog.jsonb_array_length(p_parameters->'bucket_ends') <> 12 THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Workspace Project annotation series requires twelve endpoints';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(p_parameters->'project_ids') AS item(value)
                WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                   OR (item.value #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            ) OR EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(p_parameters->'bucket_ends') AS item(value)
                WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project series parameters';
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg((item.value #>> '{}')::uuid ORDER BY item.ordinality),
                ARRAY[]::uuid[]
            )
            INTO v_project_ids
            FROM pg_catalog.jsonb_array_elements(p_parameters->'project_ids')
                WITH ORDINALITY AS item(value, ordinality);
            BEGIN
                SELECT pg_catalog.array_agg((item.value #>> '{}')::timestamptz ORDER BY item.ordinality)
                INTO v_bucket_ends
                FROM pg_catalog.jsonb_array_elements(p_parameters->'bucket_ends')
                    WITH ORDINALITY AS item(value, ordinality);
            EXCEPTION WHEN invalid_text_representation
                         OR invalid_datetime_format
                         OR datetime_field_overflow THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project series endpoints';
            END;
            SELECT COALESCE(
                pg_catalog.jsonb_build_object(
                    'total', pg_catalog.jsonb_agg(series.total_annotations::text ORDER BY series.bucket_index),
                    'ai_derived', pg_catalog.jsonb_agg(series.ai_annotations::text ORDER BY series.bucket_index)
                ),
                pg_catalog.jsonb_build_object('total', '[]'::jsonb, 'ai_derived', '[]'::jsonb)
            )
            INTO v_facts
            FROM aap_labeling_v1.workspace_project_query_annotation_series(
                v_project_ids, v_bucket_ends
            ) AS series;

        WHEN 'dataset_ids' THEN
            IF v_operation <> 'project_readiness'
               OR p_parameters IS NOT NULL
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project dataset_ids step';
            END IF;
            SELECT COALESCE(
                pg_catalog.jsonb_agg(dataset.dataset_id), '[]'::jsonb
            )
            INTO v_facts
            FROM aap_assets_v1.workspace_project_query_dataset_ids(v_target_id) AS dataset;
            v_facts := pg_catalog.jsonb_build_object('dataset_ids', v_facts);

        WHEN 'dataset_item_count' THEN
            IF v_operation <> 'project_readiness'
               OR NOT aap_workspace_project_query_v1._has_exact_keys(
                   p_parameters, ARRAY['dataset_ids']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'dataset_ids') IS DISTINCT FROM 'array'
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project dataset_item_count step';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(p_parameters->'dataset_ids') AS item(value)
                WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                   OR (item.value #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project dataset IDs';
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg((item.value #>> '{}')::uuid ORDER BY item.ordinality),
                ARRAY[]::uuid[]
            )
            INTO v_dataset_ids
            FROM pg_catalog.jsonb_array_elements(p_parameters->'dataset_ids')
                WITH ORDINALITY AS item(value, ordinality);
            SELECT aap_assets_v1.workspace_project_query_dataset_item_count(
                v_dataset_ids
            )::text INTO v_count_text;
            v_facts := pg_catalog.jsonb_build_object('count', v_count_text);

        WHEN 'linked_task_count' THEN
            IF v_operation <> 'project_readiness'
               OR NOT aap_workspace_project_query_v1._has_exact_keys(
                   p_parameters, ARRAY['dataset_ids']::text[]
               )
               OR pg_catalog.jsonb_typeof(p_parameters->'dataset_ids') IS DISTINCT FROM 'array'
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project linked_task_count step';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(p_parameters->'dataset_ids') AS item(value)
                WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                   OR (item.value #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project dataset IDs';
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg((item.value #>> '{}')::uuid ORDER BY item.ordinality),
                ARRAY[]::uuid[]
            )
            INTO v_dataset_ids
            FROM pg_catalog.jsonb_array_elements(p_parameters->'dataset_ids')
                WITH ORDINALITY AS item(value, ordinality);
            SELECT aap_labeling_v1.workspace_project_query_linked_task_count(
                v_target_id, v_dataset_ids
            )::text INTO v_count_text;
            v_facts := pg_catalog.jsonb_build_object('count', v_count_text);

        WHEN 'task_count' THEN
            IF v_operation <> 'project_readiness'
               OR p_parameters IS NOT NULL
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project task_count step';
            END IF;
            SELECT aap_labeling_v1.workspace_project_query_task_count(v_target_id)::text
            INTO v_count_text;
            v_facts := pg_catalog.jsonb_build_object('count', v_count_text);

        WHEN 'job_counts' THEN
            IF v_operation <> 'project_readiness'
               OR p_parameters IS NOT NULL
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project job_counts step';
            END IF;
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'status', jobs.status,
                        'count', jobs.job_count::text
                    )
                ),
                '[]'::jsonb
            )
            INTO v_facts
            FROM aap_jobs_v1.workspace_project_query_job_status_counts(v_target_id) AS jobs;
            v_facts := pg_catalog.jsonb_build_object('statuses', v_facts);

        WHEN 'latest_job_status' THEN
            IF v_operation <> 'project_readiness'
               OR p_parameters IS NOT NULL
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project latest_job_status step';
            END IF;
            SELECT pg_catalog.jsonb_build_object('status', latest.status)
            INTO v_facts
            FROM aap_jobs_v1.workspace_project_query_latest_job_status(v_target_id)
                AS latest;
            IF NOT FOUND THEN
                v_facts := pg_catalog.jsonb_build_object('status', NULL);
            END IF;

        WHEN 'batches' THEN
            IF v_operation <> 'project_readiness'
               OR p_parameters IS NOT NULL
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project batches step';
            END IF;
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'id', batch.id,
                        'status', batch.status,
                        'annotator_id', batch.annotator_id,
                        'reviewer_id', batch.reviewer_id
                    )
                ),
                '[]'::jsonb
            )
            INTO v_facts
            FROM aap_labeling_v1.workspace_project_query_batches(v_target_id) AS batch;
            v_facts := pg_catalog.jsonb_build_object('batches', v_facts);

        WHEN 'batch_task_counts' THEN
            IF v_operation <> 'project_readiness'
               OR p_parameters IS NOT NULL
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project batch_task_counts step';
            END IF;
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'batch_id', counts.batch_id,
                        'count', counts.task_count::text
                    )
                ),
                '[]'::jsonb
            )
            INTO v_facts
            FROM aap_labeling_v1.workspace_project_query_batch_task_counts(v_target_id)
                AS counts;
            v_facts := pg_catalog.jsonb_build_object('batch_task_counts', v_facts);

        WHEN 'active_members' THEN
            IF v_operation <> 'project_readiness'
               OR p_parameters IS NOT NULL
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project active_members step';
            END IF;
            WITH memberships AS MATERIALIZED (
                SELECT member.user_id, member.role::text AS project_role
                FROM public.project_members AS member
                WHERE member.project_id = v_target_id
            ),
            member_ids AS MATERIALIZED (
                SELECT COALESCE(
                    pg_catalog.array_agg(DISTINCT membership.user_id),
                    ARRAY[]::uuid[]
                ) AS ids
                FROM memberships AS membership
            ),
            accounts AS MATERIALIZED (
                SELECT account.id, account.is_active
                FROM aap_identity_v1.read_workspace_membership_accounts(
                    (SELECT member_ids.ids FROM member_ids)
                ) AS account
            )
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'user_id', membership.user_id,
                        'project_role', membership.project_role
                    )
                ),
                '[]'::jsonb
            )
            INTO v_facts
            FROM memberships AS membership
            JOIN accounts AS account ON account.id = membership.user_id
            WHERE account.is_active IS TRUE;
            v_facts := pg_catalog.jsonb_build_object('members', v_facts);

        WHEN 'projection_row' THEN
            IF v_operation <> 'get_project_projection'
               OR p_parameters IS NOT NULL
               OR p_status IS NOT NULL OR p_search IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project projection_row step';
            END IF;
            v_evidence_source := v_evidence->>'source';
            IF v_evidence_source = 'configuration' THEN
                IF NOT aap_workspace_project_query_v1._has_exact_keys(
                    v_evidence,
                    ARRAY['source', 'operation_id', 'operation']::text[]
                )
                   OR pg_catalog.jsonb_typeof(v_evidence->'operation_id') IS DISTINCT FROM 'string'
                   OR (v_evidence->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   OR v_evidence->>'operation' NOT IN ('create_project', 'update_project') THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'invalid Workspace Project configuration evidence';
                END IF;
                v_evidence_operation_id := (v_evidence->>'operation_id')::uuid;
                v_evidence_operation := v_evidence->>'operation';
                SELECT receipt.actor_id, receipt.operation, receipt.target_id, receipt.result
                INTO v_receipt
                FROM public.workspace_configuration_operation_receipts AS receipt
                WHERE receipt.operation_id = v_evidence_operation_id;
                v_receipt_found := FOUND;
                IF NOT v_receipt_found THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'Workspace Project configuration receipt does not bind this projection';
                END IF;
                IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
                   OR v_receipt.operation IS DISTINCT FROM v_evidence_operation
                   OR (v_evidence_operation = 'update_project'
                       AND v_receipt.target_id IS DISTINCT FROM v_target_id)
                   OR (v_evidence_operation = 'create_project'
                       AND v_receipt.target_id IS NOT NULL)
                   OR pg_catalog.jsonb_typeof(v_receipt.result) IS DISTINCT FROM 'object'
                   OR pg_catalog.jsonb_typeof(v_receipt.result->'resource_id') IS DISTINCT FROM 'string'
                   OR (v_receipt.result->>'resource_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'Workspace Project configuration receipt does not bind this projection';
                END IF;
                IF (v_receipt.result->>'resource_id')::uuid IS DISTINCT FROM v_target_id THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'Workspace Project configuration receipt does not bind this projection';
                END IF;
            ELSIF v_evidence_source = 'owner_transfer' THEN
                IF NOT aap_workspace_project_query_v1._has_exact_keys(
                    v_evidence,
                    ARRAY[
                        'source', 'operation_id', 'new_owner_id'
                    ]::text[]
                )
                   OR pg_catalog.jsonb_typeof(v_evidence->'operation_id') IS DISTINCT FROM 'string'
                   OR (v_evidence->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   OR pg_catalog.jsonb_typeof(v_evidence->'new_owner_id') IS DISTINCT FROM 'string'
                   OR (v_evidence->>'new_owner_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'invalid Workspace Project owner-transfer evidence';
                END IF;
                v_evidence_operation_id := (v_evidence->>'operation_id')::uuid;
                v_evidence_new_owner_id := (v_evidence->>'new_owner_id')::uuid;
                SELECT receipt.actor_id, receipt.project_id, receipt.new_owner_id, receipt.result
                INTO v_receipt
                FROM public.workspace_owner_transfer_operation_receipts AS receipt
                WHERE receipt.operation_id = v_evidence_operation_id;
                v_receipt_found := FOUND;
                IF NOT v_receipt_found THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'Workspace Project transfer receipt does not bind this projection';
                END IF;
                IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
                   OR v_receipt.project_id IS DISTINCT FROM v_target_id
                   OR v_receipt.new_owner_id IS DISTINCT FROM v_evidence_new_owner_id
                   OR NOT aap_workspace_project_query_v1._has_exact_keys(
                       v_receipt.result,
                       ARRAY[
                           'schema', 'ok', 'operation_id', 'actor_id', 'project_id',
                           'new_owner_id', 'data'
                       ]::text[]
                   )
                   OR pg_catalog.jsonb_typeof(v_receipt.result->'schema') IS DISTINCT FROM 'string'
                   OR v_receipt.result->>'schema' IS DISTINCT FROM 'aap-workspace-owner-transfer/v1'
                   OR v_receipt.result->'ok' IS DISTINCT FROM 'true'::jsonb
                   OR pg_catalog.jsonb_typeof(v_receipt.result->'operation_id') IS DISTINCT FROM 'string'
                   OR (v_receipt.result->>'operation_id') IS DISTINCT FROM v_evidence_operation_id::text
                   OR pg_catalog.jsonb_typeof(v_receipt.result->'actor_id') IS DISTINCT FROM 'string'
                   OR (v_receipt.result->>'actor_id') IS DISTINCT FROM v_actor_id::text
                   OR pg_catalog.jsonb_typeof(v_receipt.result->'project_id') IS DISTINCT FROM 'string'
                   OR (v_receipt.result->>'project_id') IS DISTINCT FROM v_target_id::text
                   OR pg_catalog.jsonb_typeof(v_receipt.result->'new_owner_id') IS DISTINCT FROM 'string'
                   OR (v_receipt.result->>'new_owner_id') IS DISTINCT FROM v_evidence_new_owner_id::text THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'Workspace Project transfer receipt does not bind this projection';
                END IF;
                v_transfer_result_data := v_receipt.result->'data';
                IF NOT aap_workspace_project_query_v1._has_exact_keys(
                    v_transfer_result_data,
                    ARRAY['previous_owner_id', 'updated_at', 'changed']::text[]
                )
                   OR pg_catalog.jsonb_typeof(v_transfer_result_data->'previous_owner_id') NOT IN ('null', 'string')
                   OR (pg_catalog.jsonb_typeof(v_transfer_result_data->'previous_owner_id') = 'string'
                       AND (v_transfer_result_data->>'previous_owner_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
                   OR pg_catalog.jsonb_typeof(v_transfer_result_data->'updated_at') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(v_transfer_result_data->'changed') IS DISTINCT FROM 'boolean' THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'Workspace Project transfer result data is invalid';
                END IF;
                IF pg_catalog.jsonb_typeof(v_transfer_result_data->'previous_owner_id') = 'string' THEN
                    v_transfer_previous_owner_id :=
                        (v_transfer_result_data->>'previous_owner_id')::uuid;
                ELSE
                    v_transfer_previous_owner_id := NULL;
                END IF;
                BEGIN
                    v_transfer_updated_at :=
                        (v_transfer_result_data->>'updated_at')::timestamptz;
                EXCEPTION WHEN invalid_text_representation
                             OR invalid_datetime_format
                             OR datetime_field_overflow THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'Workspace Project transfer result timestamp is invalid';
                END;
                IF NOT pg_catalog.isfinite(v_transfer_updated_at) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'Workspace Project transfer result timestamp is invalid';
                END IF;
                v_transfer_changed :=
                    (v_transfer_result_data->>'changed')::boolean;
                IF v_transfer_changed IS DISTINCT FROM
                   (v_transfer_previous_owner_id IS DISTINCT FROM v_evidence_new_owner_id) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'Workspace Project transfer result change flag is invalid';
                END IF;
            ELSIF v_evidence_source = 'class_rename' THEN
                IF NOT aap_workspace_project_query_v1._has_exact_keys(
                    v_evidence,
                    ARRAY[
                        'source', 'operation_id', 'source_operation', 'actor_id',
                        'project_id', 'owner_id', 'platform_role', 'is_active'
                    ]::text[]
                )
                   OR pg_catalog.jsonb_typeof(v_evidence->'operation_id') IS DISTINCT FROM 'string'
                   OR (v_evidence->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   OR v_evidence->>'source_operation' IS DISTINCT FROM 'rename_class'
                   OR pg_catalog.jsonb_typeof(v_evidence->'actor_id') IS DISTINCT FROM 'string'
                   OR (v_evidence->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   OR pg_catalog.jsonb_typeof(v_evidence->'project_id') IS DISTINCT FROM 'string'
                   OR (v_evidence->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   OR pg_catalog.jsonb_typeof(v_evidence->'owner_id') IS DISTINCT FROM 'string'
                   OR (v_evidence->>'owner_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   OR pg_catalog.jsonb_typeof(v_evidence->'platform_role') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(v_evidence->'is_active') IS DISTINCT FROM 'boolean' THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'invalid Workspace Project class-rename evidence';
                END IF;
                v_evidence_operation_id := (v_evidence->>'operation_id')::uuid;
                v_evidence_actor_id := (v_evidence->>'actor_id')::uuid;
                v_evidence_project_id := (v_evidence->>'project_id')::uuid;
                v_evidence_owner_id := (v_evidence->>'owner_id')::uuid;
                v_evidence_role := v_evidence->>'platform_role';
                v_evidence_active := (v_evidence->>'is_active')::boolean;
                IF v_evidence_actor_id IS DISTINCT FROM v_actor_id
                   OR v_evidence_project_id IS DISTINCT FROM v_target_id
                   OR NOT v_evidence_active
                   OR NOT (
                       v_evidence_role = 'super_admin'
                       OR (v_evidence_role = 'project_admin'
                           AND v_evidence_owner_id = v_evidence_actor_id)
                   ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'Workspace Project class-rename context does not authorize this projection';
                END IF;
            ELSE
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project projection evidence source';
            END IF;

        ELSE
            NULL;
    END CASE;

    IF p_action IN (
        'list_rows', 'visible_rows', 'get_row', 'projection_row', 'search_rows'
    ) THEN
        IF p_action <> 'list_rows' AND p_parameters IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace Project row actions do not accept parameters';
        END IF;
        IF p_action = 'list_rows' THEN
            IF v_operation NOT IN ('list_projects', 'query_projects') THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'Project list action does not match operation';
            END IF;
        ELSIF p_action = 'search_rows' THEN
            IF v_operation <> 'search_projects'
               OR p_status IS NOT NULL
               OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
               OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
               OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
               OR p_offset IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Workspace Project search-row binds';
            END IF;
        ELSIF p_status IS NOT NULL OR p_search IS NOT NULL
           OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
           OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
           OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
           OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'unused Workspace Project query binds must be null';
        END IF;

            WITH selected_projects AS MATERIALIZED (
                SELECT project.id,
                       project.created_at,
                       project.updated_at,
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
                           'guide_assets_revision', project.guide_assets_revision,
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
                       ) AS workspace_fields
                FROM public.projects AS project
                WHERE (
                    (
                        p_action = 'visible_rows'
                        AND (
                            v_actor_role = 'super_admin'
                            OR (v_actor_role = 'project_admin'
                                AND project.owner_id = v_actor_id)
                            OR EXISTS (
                                SELECT 1
                                FROM public.project_members AS membership
                                WHERE membership.project_id = project.id
                                  AND membership.user_id = v_actor_id
                            )
                        )
                    )
                    OR (
                        p_action = 'list_rows'
                        AND (
                            v_actor_role = 'super_admin'
                            OR (v_actor_role = 'project_admin'
                                AND project.owner_id = v_actor_id)
                            OR EXISTS (
                                SELECT 1
                                FROM public.project_members AS membership
                                WHERE membership.project_id = project.id
                                  AND membership.user_id = v_actor_id
                            )
                        )
                        AND (COALESCE(p_status, '') = '' OR project.status = p_status)
                        AND (COALESCE(p_search, '') = '' OR project.name ILIKE '%' || p_search || '%')
                        AND (COALESCE(pg_catalog.cardinality(p_type_keys), 0) = 0
                             OR project.type_key = ANY(p_type_keys))
                        AND (COALESCE(pg_catalog.cardinality(p_data_types), 0) = 0
                             OR project.data_type = ANY(p_data_types))
                        AND (p_member_id IS NULL OR EXISTS (
                            SELECT 1
                            FROM public.project_members AS member_filter
                            WHERE member_filter.project_id = project.id
                              AND member_filter.user_id = p_member_id
                        ))
                        AND (COALESCE(p_project_role, '') = '' OR EXISTS (
                            SELECT 1
                            FROM public.project_members AS role_filter
                            WHERE role_filter.project_id = project.id
                              AND role_filter.user_id = v_actor_id
                              AND role_filter.role = p_project_role
                        ))
                        AND (p_created_from IS NULL OR project.created_at >= p_created_from)
                        AND (p_created_to IS NULL OR project.created_at <= p_created_to)
                    )
                    OR (
                        p_action = 'search_rows'
                        AND (
                            v_actor_role = 'super_admin'
                            OR (v_actor_role = 'project_admin'
                                AND project.owner_id = v_actor_id)
                            OR EXISTS (
                                SELECT 1
                                FROM public.project_members AS membership
                                WHERE membership.project_id = project.id
                                  AND membership.user_id = v_actor_id
                            )
                        )
                        AND project.name ILIKE '%' || p_search || '%'
                    )
                    OR (
                        p_action IN ('get_row', 'projection_row')
                        AND project.id = v_target_id
                    )
                )
                ORDER BY
                    CASE WHEN p_action = 'list_rows' THEN project.created_at END DESC,
                    CASE WHEN p_action = 'list_rows' THEN project.id END DESC,
                    CASE WHEN p_action = 'search_rows' THEN project.updated_at END DESC
                LIMIT CASE
                    WHEN p_action IN ('list_rows', 'search_rows') THEN p_limit
                    ELSE NULL
                END
                OFFSET CASE
                    WHEN p_action = 'list_rows' THEN COALESCE(p_offset, 0)
                    ELSE 0
                END
            ),
            selected_ids AS MATERIALIZED (
                SELECT pg_catalog.array_agg(
                    selected.id ORDER BY selected.created_at DESC, selected.id DESC
                ) AS ids
                FROM selected_projects AS selected
                HAVING pg_catalog.count(*) > 0
            ),
            labeling_counters AS MATERIALIZED (
                SELECT counter.*
                FROM selected_ids AS selected
                CROSS JOIN LATERAL
                    aap_labeling_v1.workspace_project_query_counters(selected.ids)
                    AS counter
            ),
            row_facts AS MATERIALIZED (
                SELECT selected.id,
                       selected.created_at,
                       selected.updated_at,
                       aap_workspace_project_query_v1._project_row_json(
                           selected.workspace_fields,
                           counter.total_tasks,
                           counter.completed_tasks,
                           counter.review_tasks,
                           counter.in_progress_tasks,
                           counter.batch_summary::text
                       ) AS row_json
                FROM selected_projects AS selected
                JOIN labeling_counters AS counter
                  ON counter.project_id = selected.id
            )
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    row_facts.row_json::jsonb
                    ORDER BY
                        CASE WHEN p_action = 'list_rows' THEN row_facts.created_at END DESC,
                        CASE WHEN p_action = 'list_rows' THEN row_facts.id END DESC,
                        CASE WHEN p_action = 'search_rows' THEN row_facts.updated_at END DESC
                ),
                '[]'::jsonb
            )
            INTO v_result
            FROM row_facts;
            v_rows_json := v_result::text;
            IF p_action IN ('get_row', 'projection_row') THEN
                IF pg_catalog.jsonb_array_length(v_result) = 0 THEN
                    v_row_json := NULL;
                ELSE
                    v_row_json := (v_result->0)::text;
                END IF;
                v_facts := pg_catalog.jsonb_build_object(
                    'row_json', CASE
                        WHEN v_row_json IS NULL THEN NULL
                        ELSE pg_catalog.to_jsonb(v_row_json)
                    END
                );
            ELSE
                v_facts := pg_catalog.jsonb_build_object('rows_json', v_rows_json);
            END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-project-query/v1',
        'request', p_request,
        'action', p_action,
        'parameters', p_parameters,
        'facts', v_facts
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_project_query_v1._project_query_step_impl(
        jsonb, text, jsonb, text, text, text[], text[], uuid, text,
        timestamptz, timestamptz, bigint, integer
    )
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE VIEW aap_workspace_v1.project_facts
WITH (security_barrier = true)
AS
SELECT
    project.id,
    project.organization_id,
    project.display_id,
    project.name,
    project.type_label,
    project.type_key,
    project.data_type,
    project.owner_id,
    project.status,
    project.ai_enabled,
    project.ml_backend_pool_id,
    project.default_variants,
    project.preannotate_pipeline,
    project.label_config,
    project.tool_bindings,
    project.sampling,
    project.maximum_annotations,
    project.show_overlap_first,
    project.iou_dedup_threshold,
    project.box_threshold,
    project.text_threshold,
    project.ai_interactive_enabled,
    project.raster_mask_native_editing_enabled,
    project.mask_qc_config,
    project.point_cloud_quality_config,
    project.rendering_config,
    project.annotation_guide,
    project.guide_assets,
    project.model_version,
    project.task_lock_ttl_seconds,
    project.video_sampling,
    project.video_collaboration,
    project.scene_mode,
    project.prefer_same_scene_continuation,
    project.scene_continuation_window_min,
    project.due_date,
    project.created_at,
    project.updated_at,
    project.guide_assets_revision
FROM public.projects AS project;
-- aap-statement-break
REVOKE ALL ON aap_workspace_v1.project_facts FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE VIEW aap_workspace_v1.project_catalog_configs
WITH (security_barrier = true)
AS
SELECT
    project.id AS project_id,
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
        'tool_bindings', project.tool_bindings,
        'label_config', project.label_config,
        'sampling', project.sampling,
        'maximum_annotations', project.maximum_annotations,
        'show_overlap_first', project.show_overlap_first,
        'iou_dedup_threshold', project.iou_dedup_threshold::double precision,
        'box_threshold', project.box_threshold::double precision,
        'text_threshold', project.text_threshold::double precision,
        'ai_interactive_enabled', project.ai_interactive_enabled,
        'raster_mask_native_editing_enabled', project.raster_mask_native_editing_enabled,
        'mask_qc_config', project.mask_qc_config,
        'point_cloud_quality_config', project.point_cloud_quality_config,
        'rendering_config', project.rendering_config,
        'video_sampling', project.video_sampling,
        'video_collaboration', project.video_collaboration,
        'scene_mode', project.scene_mode,
        'prefer_same_scene_continuation', project.prefer_same_scene_continuation,
        'scene_continuation_window_min', project.scene_continuation_window_min,
        'default_variants', project.default_variants,
        'preannotate_pipeline', project.preannotate_pipeline,
        'annotation_guide', project.annotation_guide,
        'guide_assets', project.guide_assets,
        'guide_assets_revision', project.guide_assets_revision,
        'model_version', project.model_version,
        'task_lock_ttl_seconds', project.task_lock_ttl_seconds,
        'due_date', project.due_date,
        'created_at', pg_catalog.to_char(
            project.created_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.US'
        ) || '+00:00',
        'updated_at', pg_catalog.to_char(
            project.updated_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.US'
        ) || '+00:00'
    ) AS project_config
FROM public.projects AS project;
-- aap-statement-break
REVOKE ALL ON aap_workspace_v1.project_catalog_configs FROM PUBLIC;
