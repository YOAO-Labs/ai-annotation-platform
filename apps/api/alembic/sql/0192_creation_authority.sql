-- Versioned owner capabilities for dataset-to-project task creation.
-- Install independently with psql --single-transaction and ON_ERROR_STOP enabled.
CREATE SCHEMA aap_identity_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_v1 FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.lock_active_actor(p_actor_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor_id uuid;
    v_platform_role text;
    v_is_active boolean;
BEGIN
    IF p_actor_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-active-actor/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'account_not_active')
        );
    END IF;

    BEGIN
        SELECT actor.id, actor.role, actor.is_active
        INTO v_actor_id, v_platform_role, v_is_active
        FROM public.users AS actor
        WHERE actor.id = p_actor_id
        FOR SHARE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-active-actor/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'account_busy')
            );
    END;

    IF NOT FOUND OR NOT v_is_active THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-active-actor/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'account_not_active')
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-active-actor/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', v_actor_id,
            'platform_role', v_platform_role
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.lock_active_actor(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.lock_project_manager(
    p_actor_id uuid,
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
    v_actor jsonb;
    v_platform_role text;
    v_project_id uuid;
    v_owner_id uuid;
    v_data_type text;
    v_scene_mode boolean;
    v_member_role text;
BEGIN
    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor ->> 'ok' IS DISTINCT FROM 'true' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-manager/v1',
            'ok', false,
            'error', v_actor -> 'error'
        );
    END IF;
    v_platform_role := v_actor #>> '{data,platform_role}';

    BEGIN
        SELECT project.id, project.owner_id, project.data_type, project.scene_mode
        INTO v_project_id, v_owner_id, v_data_type, v_scene_mode
        FROM public.projects AS project
        WHERE project.id = p_project_id
        FOR SHARE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-project-manager/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'project_busy')
            );
    END;

    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-manager/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'project_missing')
        );
    END IF;

    IF v_platform_role IS NULL
       OR v_platform_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-manager/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_account_role')
        );
    END IF;

    IF v_platform_role = 'super_admin'
       OR (v_platform_role = 'project_admin' AND v_owner_id = p_actor_id) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-manager/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'actor_id', p_actor_id,
                'platform_role', v_platform_role,
                'project_id', v_project_id,
                'owner_id', v_owner_id,
                'data_type', v_data_type,
                'scene_mode', v_scene_mode
            )
        );
    END IF;

    BEGIN
        SELECT member.role
        INTO v_member_role
        FROM public.project_members AS member
        WHERE member.project_id = v_project_id
          AND member.user_id = p_actor_id
        FOR SHARE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-project-manager/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'membership_busy')
            );
    END;

    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-manager/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'project_missing')
        );
    END IF;

    IF v_member_role IS NULL OR v_member_role NOT IN ('annotator', 'reviewer', 'viewer') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-manager/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_project_role')
        );
    END IF;

    IF v_platform_role = 'viewer' AND v_member_role <> 'viewer' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-manager/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'membership_incompatible')
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-project-manager/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', 'project_manager_required')
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.lock_project_manager(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.associate_project(
    p_actor_id uuid,
    p_dataset_id uuid,
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
    v_manager jsonb;
    v_dataset_id uuid;
    v_project_data_type text;
    v_dataset_data_type text;
    v_project_scene_mode boolean;
    v_dataset_has_scenes boolean;
    v_link_id uuid;
    v_existing boolean;
    v_item_count bigint;
    v_project_media text;
    v_dataset_media text;
    v_facts jsonb;
BEGIN
    -- Take the parent before authority locks that also conflict with counters.
    v_dataset_id := aap_assets_v1.lock_dataset_reference(p_dataset_id);
    v_manager := aap_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF v_manager ->> 'ok' IS DISTINCT FROM 'true' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-dataset-association/v1',
            'ok', false,
            'error', v_manager -> 'error'
        );
    END IF;

    v_project_data_type := v_manager #>> '{data,data_type}';
    v_project_scene_mode := (v_manager #>> '{data,scene_mode}')::boolean;

    IF v_dataset_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-dataset-association/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'dataset_missing')
        );
    END IF;

    SELECT association.id
    INTO v_link_id
    FROM public.project_datasets AS association
    WHERE association.dataset_id = v_dataset_id
      AND association.project_id = p_project_id;

    IF FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-dataset-association/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'link_id', v_link_id,
                'existing', true,
                'item_count', 0,
                'project_data_type', v_project_data_type
            )
        );
    END IF;

    SELECT dataset.data_type
    INTO v_dataset_data_type
    FROM public.datasets AS dataset
    WHERE dataset.id = v_dataset_id;

    SELECT EXISTS (
        SELECT 1
        FROM public.scenes AS scene
        WHERE scene.dataset_id = v_dataset_id
    )
    INTO v_dataset_has_scenes;

    v_project_media := CASE
        WHEN v_project_data_type IN ('lidar', 'point_cloud', 'pointcloud') THEN 'lidar'
        ELSE COALESCE(NULLIF(v_project_data_type, ''), 'image')
    END;
    v_dataset_media := CASE
        WHEN v_dataset_data_type IN ('lidar', 'point_cloud', 'pointcloud') THEN 'lidar'
        ELSE COALESCE(NULLIF(v_dataset_data_type, ''), 'image')
    END;
    v_facts := pg_catalog.jsonb_build_object(
        'project_data_type', v_project_data_type,
        'dataset_data_type', v_dataset_data_type,
        'project_scene_mode', v_project_scene_mode,
        'dataset_has_scenes', v_dataset_has_scenes
    );

    IF v_project_media IS DISTINCT FROM v_dataset_media THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-dataset-association/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'media_kind_mismatch',
                'details', v_facts
            )
        );
    END IF;

    IF v_project_scene_mode IS DISTINCT FROM v_dataset_has_scenes THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-dataset-association/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'scene_kind_mismatch',
                'details', v_facts
            )
        );
    END IF;

    v_link_id := pg_catalog.gen_random_uuid();
    INSERT INTO public.project_datasets (id, project_id, dataset_id)
    VALUES (v_link_id, p_project_id, v_dataset_id);

    SELECT count(*)
    INTO v_item_count
    FROM public.dataset_items AS item
    WHERE item.dataset_id = v_dataset_id;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-association/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'link_id', v_link_id,
            'existing', false,
            'item_count', v_item_count,
            'project_data_type', v_project_data_type
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.associate_project(uuid, uuid, uuid) FROM PUBLIC;
