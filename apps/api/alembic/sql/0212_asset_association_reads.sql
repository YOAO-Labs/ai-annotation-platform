-- Stable owner projections and one-statement Assets association reads.
-- Install through the adjacent Alembic migration or psql --single-transaction.
CREATE FUNCTION aap_identity_v1.read_actor_facts(p_actor_id uuid)
RETURNS TABLE(actor_id uuid, platform_role text, is_active boolean)
LANGUAGE sql
STABLE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT account.id, account.role, account.is_active
    FROM public.users AS account
    WHERE account.id = p_actor_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.read_actor_facts(uuid) FROM PUBLIC;
-- aap-statement-break
COMMENT ON FUNCTION aap_identity_v1.read_actor_facts(uuid) IS
    'Stable active-account facts for same-statement read authorization; exposes no email or profile fields.';
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1._project_read_failure(p_code text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-project-visibility/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1._project_read_failure(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.read_project_visibility(
    p_actor_id uuid,
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor record;
    v_owner_id uuid;
    v_project_role text;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL THEN
        RETURN aap_workspace_v1._project_read_failure('invalid_request');
    END IF;

    SELECT actor_facts.actor_id, actor_facts.platform_role, actor_facts.is_active
    INTO v_actor
    FROM aap_identity_v1.read_actor_facts(p_actor_id) AS actor_facts;
    IF NOT FOUND THEN
        RETURN aap_workspace_v1._project_read_failure('account_not_active');
    END IF;
    IF v_actor.actor_id IS DISTINCT FROM p_actor_id OR v_actor.is_active IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'Workspace association Identity facts mismatch';
    END IF;
    IF NOT v_actor.is_active THEN
        RETURN aap_workspace_v1._project_read_failure('account_not_active');
    END IF;

    SELECT project.owner_id
    INTO v_owner_id
    FROM public.projects AS project
    WHERE project.id = p_project_id;
    IF NOT FOUND THEN
        RETURN aap_workspace_v1._project_read_failure('project_missing');
    END IF;
    IF v_actor.platform_role IS NULL OR v_actor.platform_role NOT IN (
        'super_admin', 'project_admin', 'employee', 'viewer'
    ) THEN
        RETURN aap_workspace_v1._project_read_failure('invalid_account_role');
    END IF;

    IF v_actor.platform_role = 'super_admin'
       OR (v_actor.platform_role = 'project_admin' AND v_owner_id = p_actor_id) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-visibility/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'actor_id', p_actor_id,
                'project_id', p_project_id
            )
        );
    END IF;

    SELECT membership.role
    INTO v_project_role
    FROM public.project_members AS membership
    WHERE membership.project_id = p_project_id
      AND membership.user_id = p_actor_id;
    IF NOT FOUND THEN
        RETURN aap_workspace_v1._project_read_failure('project_missing');
    END IF;
    IF v_project_role IS NULL OR v_project_role NOT IN (
        'annotator', 'reviewer', 'viewer'
    ) THEN
        RETURN aap_workspace_v1._project_read_failure('invalid_project_role');
    END IF;
    IF v_actor.platform_role = 'viewer' AND v_project_role <> 'viewer' THEN
        RETURN aap_workspace_v1._project_read_failure('incompatible_project_role');
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-project-visibility/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'project_id', p_project_id
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.read_project_visibility(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE VIEW aap_workspace_v1.project_catalog_configs
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
-- aap-statement-break
COMMENT ON VIEW aap_workspace_v1.project_catalog_configs IS
    'Workspace-owned raw ProjectOut configuration fields; omits Labeling counters, enrichment and ML pool ID.';
-- aap-statement-break
CREATE VIEW aap_labeling_v1.project_catalog_counters
WITH (security_barrier = true)
AS
SELECT
    project.id AS project_id,
    COALESCE(project.total_tasks, 0) AS total_tasks,
    COALESCE(project.completed_tasks, 0) AS completed_tasks,
    COALESCE(project.review_tasks, 0) AS review_tasks,
    COALESCE(project.in_progress_tasks, 0) AS in_progress_tasks,
    COALESCE(project.batch_summary, '{}'::jsonb) AS batch_summary
FROM public.projects AS project;
-- aap-statement-break
REVOKE ALL ON aap_labeling_v1.project_catalog_counters FROM PUBLIC;
-- aap-statement-break
COMMENT ON VIEW aap_labeling_v1.project_catalog_counters IS
    'Labeling-owned materialized Project counters; reads persisted values without recounting Tasks or batches.';
-- aap-statement-break
CREATE VIEW aap_assets_v1.project_dataset_item_sources
WITH (security_barrier = true)
AS
SELECT
    association.project_id,
    association.dataset_id,
    item.id AS item_id
FROM public.project_datasets AS association
JOIN public.dataset_items AS item
  ON item.dataset_id = association.dataset_id;
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.project_dataset_item_sources FROM PUBLIC;
-- aap-statement-break
COMMENT ON VIEW aap_assets_v1.project_dataset_item_sources IS
    'Narrow Assets source projection for Labeling: only project, dataset, and DatasetItem UUIDs.';
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.project_dataset_task_counts(p_project_id uuid)
RETURNS TABLE(dataset_id uuid, tasks_in_project bigint)
LANGUAGE sql
STABLE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT source.dataset_id, pg_catalog.count(task.id)::bigint AS tasks_in_project
    FROM aap_assets_v1.project_dataset_item_sources AS source
    JOIN public.tasks AS task
      ON task.dataset_item_id = source.item_id
     AND task.project_id = p_project_id
    WHERE source.project_id = p_project_id
    GROUP BY source.dataset_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.project_dataset_task_counts(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._association_read_failure(p_code text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-associations/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._association_read_failure(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.list_dataset_projects(
    p_actor_id uuid,
    p_dataset_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor record;
    v_projects jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_id IS NULL THEN
        RETURN aap_assets_v1._association_read_failure('invalid_request');
    END IF;

    SELECT facts.actor_id, facts.platform_role, facts.is_active
    INTO v_actor
    FROM aap_identity_v1.read_actor_facts(p_actor_id) AS facts;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._association_read_failure('account_not_active');
    END IF;
    IF v_actor.actor_id IS DISTINCT FROM p_actor_id OR v_actor.is_active IS NULL
       OR v_actor.platform_role IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'Assets association Identity facts mismatch';
    END IF;
    IF NOT v_actor.is_active THEN
        RETURN aap_assets_v1._association_read_failure('account_not_active');
    END IF;

    WITH associations AS MATERIALIZED (
        SELECT
            association.project_id,
            association.id AS association_id,
            association.created_at
        FROM public.project_datasets AS association
        WHERE association.dataset_id = p_dataset_id
    )
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            config.project_config || pg_catalog.jsonb_build_object(
                'total_tasks', counters.total_tasks,
                'completed_tasks', counters.completed_tasks,
                'review_tasks', counters.review_tasks,
                'in_progress_tasks', counters.in_progress_tasks,
                'batch_summary', counters.batch_summary
            ) ORDER BY associations.created_at DESC, associations.association_id DESC
        ),
        '[]'::jsonb
    )
    INTO v_projects
    FROM associations
    JOIN aap_workspace_v1.project_catalog_configs AS config
      ON config.project_id = associations.project_id
    JOIN aap_labeling_v1.project_catalog_counters AS counters
      ON counters.project_id = associations.project_id;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-associations/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'dataset_id', p_dataset_id,
            'projects', v_projects
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.list_dataset_projects(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.list_project_datasets(
    p_actor_id uuid,
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_visibility jsonb;
    v_code text;
    v_datasets jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL THEN
        RETURN aap_assets_v1._association_read_failure('invalid_request');
    END IF;

    v_visibility := aap_workspace_v1.read_project_visibility(p_actor_id, p_project_id);
    IF v_visibility ->> 'schema' IS DISTINCT FROM 'aap-project-visibility/v1'
       OR pg_catalog.jsonb_typeof(v_visibility -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'Assets association Workspace contract mismatch';
    END IF;
    IF v_visibility -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_visibility #>> '{error,code}';
        IF pg_catalog.jsonb_typeof(v_visibility -> 'error') IS DISTINCT FROM 'object'
           OR ((v_visibility -> 'error') - ARRAY['code']) <> '{}'::jsonb
           OR v_code IS NULL OR v_code NOT IN (
                'invalid_request', 'account_not_active', 'invalid_account_role',
                'project_missing', 'invalid_project_role', 'incompatible_project_role'
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'Assets association Workspace failure mismatch';
        END IF;
        RETURN aap_assets_v1._association_read_failure(v_code);
    END IF;
    IF pg_catalog.jsonb_typeof(v_visibility -> 'data') IS DISTINCT FROM 'object'
       OR ((v_visibility -> 'data') - ARRAY['actor_id', 'project_id']) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(v_visibility #> '{data,actor_id}') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_visibility #> '{data,project_id}') IS DISTINCT FROM 'string'
       OR v_visibility #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR v_visibility #>> '{data,project_id}' IS DISTINCT FROM p_project_id::text THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'Assets association Workspace facts mismatch';
    END IF;

    WITH associations AS MATERIALIZED (
        SELECT
            association.dataset_id,
            association.id AS association_id,
            association.created_at
        FROM public.project_datasets AS association
        WHERE association.project_id = p_project_id
    ),
    dataset_rows AS MATERIALIZED (
        SELECT
            dataset.id,
            dataset.display_id,
            dataset.name,
            dataset.data_type,
            associations.association_id,
            associations.created_at
        FROM associations
        JOIN public.datasets AS dataset
          ON dataset.id = associations.dataset_id
    ),
    item_counts AS (
        SELECT item.dataset_id, pg_catalog.count(*)::bigint AS items_count
        FROM public.dataset_items AS item
        JOIN dataset_rows ON dataset_rows.id = item.dataset_id
        GROUP BY item.dataset_id
    ),
    task_counts AS (
        SELECT counts.dataset_id, counts.tasks_in_project
        FROM aap_labeling_v1.project_dataset_task_counts(p_project_id) AS counts
    )
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'id', dataset_rows.id,
                'display_id', dataset_rows.display_id,
                'name', dataset_rows.name,
                'data_type', dataset_rows.data_type,
                'linked_at', CASE
                    WHEN dataset_rows.created_at IS NULL THEN NULL
                    ELSE pg_catalog.to_char(
                        dataset_rows.created_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US'
                    ) || '+00:00'
                END,
                'items_count', COALESCE(item_counts.items_count, 0),
                'tasks_in_project', COALESCE(task_counts.tasks_in_project, 0)
            ) ORDER BY dataset_rows.created_at DESC, dataset_rows.association_id DESC
        ),
        '[]'::jsonb
    )
    INTO v_datasets
    FROM dataset_rows
    LEFT JOIN item_counts ON item_counts.dataset_id = dataset_rows.id
    LEFT JOIN task_counts ON task_counts.dataset_id = dataset_rows.id;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-associations/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'project_id', p_project_id,
            'datasets', v_datasets
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.list_project_datasets(uuid, uuid) FROM PUBLIC;
