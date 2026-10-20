-- Typed cross-owner facts/effect used by the Workspace configuration workflow.
CREATE FUNCTION aap_identity_v1.workspace_configuration_org_scope(
    p_actor_id uuid,
    p_organization_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_visible boolean;
BEGIN
    IF p_actor_id IS NULL OR p_organization_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration organization scope';
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM public.organization_members AS membership
        WHERE membership.user_id = p_actor_id
          AND membership.organization_id = p_organization_id
          AND membership.deleted_at IS NULL
    ) INTO v_visible;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-workspace-configuration/v1',
        'actor_id', p_actor_id,
        'organization_id', p_organization_id,
        'visible', v_visible
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_v1.workspace_configuration_org_scope(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.workspace_configuration_datasets(
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
                'data_type', dataset.data_type,
                'has_scenes', EXISTS (
                    SELECT 1
                    FROM public.scenes AS scene
                    WHERE scene.dataset_id = dataset.id
                )
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
    aap_assets_v1.workspace_configuration_datasets(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.workspace_configuration_video_facts(
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
    v_annotation_count bigint;
    v_active_tracker_count bigint;
    v_dataset_item_ids jsonb;
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration project';
    END IF;

    SELECT count(*)::bigint
    INTO v_annotation_count
    FROM public.annotations AS annotation
    WHERE annotation.project_id = p_project_id
      AND annotation.is_active IS TRUE
      AND annotation.was_cancelled IS FALSE;

    SELECT count(*)::bigint
    INTO v_active_tracker_count
    FROM public.video_tracker_jobs AS tracker
    JOIN public.tasks AS task ON task.id = tracker.task_id
    WHERE task.project_id = p_project_id
      AND tracker.status IN ('queued', 'running');

    SELECT COALESCE(
        pg_catalog.jsonb_agg(pg_catalog.to_jsonb(item.dataset_item_id)),
        '[]'::jsonb
    )
    INTO v_dataset_item_ids
    FROM (
        SELECT DISTINCT task.dataset_item_id
        FROM public.tasks AS task
        WHERE task.project_id = p_project_id
          AND task.dataset_item_id IS NOT NULL
    ) AS item;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-workspace-configuration/v1',
        'project_id', p_project_id,
        'annotation_count', v_annotation_count,
        'active_tracker_count', v_active_tracker_count,
        'dataset_item_ids', v_dataset_item_ids
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_configuration_video_facts(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_v1.enable_workspace_project_backend(
    p_project_id uuid,
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
    v_pool_id uuid;
    v_pool_enabled boolean;
    v_association_id uuid;
    v_association_enabled boolean;
BEGIN
    IF p_project_id IS NULL OR p_registry_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration backend';
    END IF;

    SELECT member.pool_id
    INTO v_pool_id
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id;

    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-inference-workspace-configuration/v1',
            'project_id', p_project_id,
            'registry_id', p_registry_id,
            'pool_id', NULL
        );
    END IF;

    SELECT pool.enabled
    INTO v_pool_enabled
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = v_pool_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-inference-workspace-configuration/v1',
            'project_id', p_project_id,
            'registry_id', p_registry_id,
            'pool_id', NULL
        );
    END IF;

    SELECT association.id, association.enabled
    INTO v_association_id, v_association_enabled
    FROM public.project_ml_backend_pool AS association
    WHERE association.project_id = p_project_id
      AND association.pool_id = v_pool_id;

    IF NOT FOUND THEN
        INSERT INTO public.project_ml_backend_pool (
            id, project_id, pool_id, enabled
        )
        VALUES (
            pg_catalog.gen_random_uuid(), p_project_id, v_pool_id, TRUE
        );
    ELSIF v_association_enabled IS DISTINCT FROM TRUE THEN
        UPDATE public.project_ml_backend_pool
        SET enabled = TRUE,
            updated_at = pg_catalog.now()
        WHERE id = v_association_id;
    END IF;

    IF v_pool_enabled IS DISTINCT FROM TRUE THEN
        UPDATE public.ml_backend_service_pools
        SET enabled = TRUE,
            routing_generation = routing_generation + 1,
            updated_at = pg_catalog.now()
        WHERE id = v_pool_id;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-workspace-configuration/v1',
        'project_id', p_project_id,
        'registry_id', p_registry_id,
        'pool_id', v_pool_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_v1.enable_workspace_project_backend(uuid, uuid)
    FROM PUBLIC;
