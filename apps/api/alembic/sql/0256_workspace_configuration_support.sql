-- Narrow, separately snapshotted facts used by the Workspace configuration owner.
CREATE FUNCTION aap_assets_v1.workspace_configuration_dataset_rows(
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_datasets jsonb;
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration project';
    END IF;

    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'id', dataset.id,
                'display_id', dataset.display_id,
                'data_type', dataset.data_type
            )
        ),
        '[]'::jsonb
    )
    INTO v_datasets
    FROM public.project_datasets AS association
    JOIN public.datasets AS dataset
      ON dataset.id = association.dataset_id
    WHERE association.project_id = p_project_id;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-workspace-configuration/v1',
        'project_id', p_project_id,
        'datasets', v_datasets
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_assets_v1.workspace_configuration_dataset_rows(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.workspace_configuration_dataset_scenes(
    p_dataset_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_has_scenes boolean;
BEGIN
    IF p_dataset_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration dataset';
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM public.scenes AS scene
        WHERE scene.dataset_id = p_dataset_id
    ) INTO v_has_scenes;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-workspace-configuration/v1',
        'dataset_id', p_dataset_id,
        'has_scenes', v_has_scenes
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_assets_v1.workspace_configuration_dataset_scenes(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.workspace_configuration_existing_item_ids(
    p_item_ids uuid[]
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item_ids jsonb;
BEGIN
    IF p_item_ids IS NULL OR EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(p_item_ids) AS requested(item_id)
        WHERE requested.item_id IS NULL
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration dataset item ids';
    END IF;

    SELECT COALESCE(
        pg_catalog.jsonb_agg(pg_catalog.to_jsonb(item.id)),
        '[]'::jsonb
    )
    INTO v_item_ids
    FROM (
        SELECT DISTINCT dataset_item.id
        FROM pg_catalog.unnest(p_item_ids) AS requested(item_id)
        JOIN public.dataset_items AS dataset_item
          ON dataset_item.id = requested.item_id
    ) AS item;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-workspace-configuration/v1',
        'item_ids', v_item_ids
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_assets_v1.workspace_configuration_existing_item_ids(uuid[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.workspace_configuration_task_count(
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_total_tasks bigint;
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration project';
    END IF;

    SELECT counters.total_tasks
    INTO v_total_tasks
    FROM aap_labeling_v1.project_catalog_counters AS counters
    WHERE counters.project_id = p_project_id;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-workspace-configuration/v1',
        'project_id', p_project_id,
        'total_tasks', COALESCE(v_total_tasks, 0)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_configuration_task_count(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.workspace_configuration_annotation_count(
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_annotation_count bigint;
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration project';
    END IF;

    SELECT pg_catalog.count(*)::bigint
    INTO v_annotation_count
    FROM public.annotations AS annotation
    WHERE annotation.project_id = p_project_id
      AND annotation.is_active IS TRUE
      AND annotation.was_cancelled IS FALSE;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-workspace-configuration/v1',
        'project_id', p_project_id,
        'annotation_count', v_annotation_count
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_configuration_annotation_count(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.workspace_configuration_tracker_count(
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_active_tracker_count bigint;
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration project';
    END IF;

    SELECT pg_catalog.count(*)::bigint
    INTO v_active_tracker_count
    FROM public.video_tracker_jobs AS tracker
    JOIN public.tasks AS task
      ON task.id = tracker.task_id
    WHERE task.project_id = p_project_id
      AND tracker.status IN ('queued', 'running');

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-workspace-configuration/v1',
        'project_id', p_project_id,
        'active_tracker_count', v_active_tracker_count
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_configuration_tracker_count(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.workspace_configuration_video_items(
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_task_item_ids uuid[];
    v_existing_items jsonb;
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration project';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(DISTINCT task.dataset_item_id),
        ARRAY[]::uuid[]
    )
    INTO v_task_item_ids
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
      AND task.dataset_item_id IS NOT NULL;

    v_existing_items :=
        aap_assets_v1.workspace_configuration_existing_item_ids(v_task_item_ids);

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-workspace-configuration/v1',
        'project_id', p_project_id,
        'dataset_item_ids', v_existing_items->'item_ids'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_configuration_video_items(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_v1.workspace_configuration_source_pool(
    p_pool_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_legacy_registry_id uuid;
    v_found boolean;
BEGIN
    IF p_pool_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration backend pool';
    END IF;

    SELECT pool.legacy_instance_id
    INTO v_legacy_registry_id
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id;
    v_found := FOUND;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-workspace-configuration/v1',
        'pool_id', p_pool_id,
        'legacy_registry_id', v_legacy_registry_id,
        'found', v_found
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_v1.workspace_configuration_source_pool(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_v1.workspace_configuration_backend(
    p_registry_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_backend record;
    v_backend_value jsonb;
BEGIN
    IF p_registry_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration backend';
    END IF;

    SELECT registry.id,
           registry.url,
           registry.auth_method,
           registry.auth_token,
           registry.extra_params
    INTO v_backend
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id;

    IF FOUND THEN
        v_backend_value := pg_catalog.jsonb_build_object(
            'id', v_backend.id,
            'url', v_backend.url,
            'auth_method', v_backend.auth_method,
            'auth_token', v_backend.auth_token,
            'extra_params', v_backend.extra_params
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-workspace-configuration/v1',
        'registry_id', p_registry_id,
        'backend', v_backend_value
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_v1.workspace_configuration_backend(uuid)
    FROM PUBLIC;
