-- Workspace-owned current project facts for task visibility. Labeling composes
-- these facts with its task, batch, and assignment state.
CREATE FUNCTION aap_workspace_v1.read_spatial_project_access(
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
    v_video_collaboration jsonb;
    v_project_role text;
    v_is_manager boolean := false;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-spatial-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1'
       OR v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-spatial-access/v1',
            'ok', false,
            'error', COALESCE(
                v_actor -> 'error',
                pg_catalog.jsonb_build_object('code', 'invalid_account_role')
            )
        );
    END IF;
    IF pg_catalog.jsonb_typeof(v_actor -> 'data') IS DISTINCT FROM 'object'
       OR ((v_actor -> 'data') - ARRAY['actor_id', 'platform_role']) IS DISTINCT FROM '{}'::jsonb
       OR v_actor #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,platform_role}') IS DISTINCT FROM 'string' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-spatial-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_account_role')
        );
    END IF;
    v_platform_role := v_actor #>> '{data,platform_role}';
    IF v_platform_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-spatial-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_account_role')
        );
    END IF;

    BEGIN
        SELECT project.id, project.owner_id, project.video_collaboration
        INTO v_project_id, v_owner_id, v_video_collaboration
        FROM public.projects AS project
        WHERE project.id = p_project_id
        FOR SHARE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-workspace-spatial-access/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'project_busy')
            );
    END;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-spatial-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'project_missing')
        );
    END IF;

    IF v_platform_role = 'super_admin'
       OR (v_platform_role = 'project_admin' AND v_owner_id = p_actor_id) THEN
        v_is_manager := true;
    ELSE
        BEGIN
            SELECT member.role
            INTO v_project_role
            FROM public.project_members AS member
            WHERE member.project_id = v_project_id
              AND member.user_id = p_actor_id
            FOR SHARE NOWAIT;
        EXCEPTION
            WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-workspace-spatial-access/v1',
                    'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'membership_busy')
                );
        END;
        IF NOT FOUND THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-workspace-spatial-access/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'project_missing')
            );
        END IF;
        IF v_project_role NOT IN ('annotator', 'reviewer', 'viewer')
           OR (v_platform_role = 'viewer' AND v_project_role <> 'viewer') THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-workspace-spatial-access/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_account_role')
            );
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-spatial-access/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'project_id', v_project_id,
            'platform_role', v_platform_role,
            'project_role', v_project_role,
            'is_manager', v_is_manager,
            'is_super_admin', v_platform_role = 'super_admin',
            'video_collaboration_enabled',
                COALESCE(v_video_collaboration -> 'enabled' = 'true'::jsonb, false)
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.read_spatial_project_access(uuid, uuid) FROM PUBLIC;
-- aap-statement-break

-- Labeling projects only its camera-role facts to Assets for item pages.
CREATE FUNCTION aap_labeling_v1.read_spatial_camera_roles(p_item_ids uuid[])
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_items jsonb;
    v_count integer;
    v_distinct integer;
BEGIN
    IF p_item_ids IS NULL
       OR (pg_catalog.cardinality(p_item_ids) > 0 AND pg_catalog.array_ndims(p_item_ids) IS DISTINCT FROM 1)
       OR pg_catalog.array_position(p_item_ids, NULL) IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-roles/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT pg_catalog.cardinality(p_item_ids), pg_catalog.count(DISTINCT value)
    INTO v_count, v_distinct
    FROM pg_catalog.unnest(p_item_ids) AS requested(value);
    IF COALESCE(v_count, 0) <> COALESCE(v_distinct, 0) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-roles/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'item_id', requested.item_id,
                'roles', COALESCE(roles.roles, '[]'::jsonb)
            ) ORDER BY requested.item_id
        ),
        '[]'::jsonb
    )
    INTO v_items
    FROM pg_catalog.unnest(p_item_ids) AS requested(item_id)
    LEFT JOIN LATERAL (
        SELECT pg_catalog.jsonb_agg(DISTINCT link.role ORDER BY link.role) AS roles
        FROM public.task_dataset_item_links AS link
        WHERE link.dataset_item_id = requested.item_id
          AND pg_catalog.left(link.role, 7) = 'camera_'
    ) AS roles ON true;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-spatial-camera-roles/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object('items', v_items)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_spatial_camera_roles(uuid[]) FROM PUBLIC;
-- aap-statement-break

-- Current Task visibility belongs to Labeling; current project access belongs to Workspace.
CREATE FUNCTION aap_labeling_v1.read_spatial_camera_target(
    p_actor_id uuid,
    p_task_id uuid,
    p_camera_role text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_task record;
    v_batch record;
    v_access jsonb;
    v_access_data jsonb;
    v_link_item_id uuid;
    v_actor_reply jsonb;
    v_actor_role text;
    v_visible boolean := false;
    v_assigned boolean := false;
    v_batch_status text;
    v_batch_annotator_id uuid;
BEGIN
    IF p_actor_id IS NULL OR p_task_id IS NULL
       OR p_camera_role IS NULL OR pg_catalog.char_length(p_camera_role) NOT BETWEEN 8 AND 50
       OR pg_catalog.left(p_camera_role, 7) <> 'camera_' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-target/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    v_actor_reply := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor_reply ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1'
       OR v_actor_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-target/v1',
            'ok', false,
            'error', COALESCE(
                v_actor_reply -> 'error',
                pg_catalog.jsonb_build_object('code', 'invalid_account_role')
            )
        );
    END IF;
    v_actor_role := v_actor_reply #>> '{data,platform_role}';
    IF v_actor_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-target/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_account_role')
        );
    END IF;

    BEGIN
        SELECT task.id, task.project_id, task.file_type, task.batch_id,
               task.status, task.assignee_id, task.reviewer_id
        INTO v_task
        FROM public.tasks AS task
        WHERE task.id = p_task_id
        FOR SHARE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-target/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'task_busy')
            );
    END;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-target/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'task_missing')
        );
    END IF;

    v_access := aap_workspace_v1.read_spatial_project_access(
        p_actor_id, v_task.project_id
    );
    IF v_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-spatial-access/v1' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-target/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_access #>> '{error,code}' = 'account_not_active' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-target/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'account_not_active')
            );
        ELSIF v_access #>> '{error,code}' = 'account_busy' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-target/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'account_busy')
            );
        ELSIF v_access #>> '{error,code}' = 'invalid_account_role' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-target/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_account_role')
            );
        ELSIF v_access #>> '{error,code}' IN ('project_busy', 'membership_busy') THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-target/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'task_busy')
            );
        ELSE
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-target/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'task_not_visible')
            );
        END IF;
    END IF;
    v_access_data := v_access -> 'data';
    IF pg_catalog.jsonb_typeof(v_access_data) IS DISTINCT FROM 'object'
       OR ((v_access_data) - ARRAY[
            'actor_id', 'project_id', 'platform_role', 'project_role',
            'is_manager', 'is_super_admin', 'video_collaboration_enabled'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR v_access_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_access_data ->> 'project_id' IS DISTINCT FROM v_task.project_id::text
       OR v_access_data ->> 'platform_role' IS DISTINCT FROM v_actor_role
       OR NOT (v_access_data ? 'project_role')
       OR pg_catalog.jsonb_typeof(v_access_data -> 'is_manager') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_access_data -> 'is_super_admin') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_access_data -> 'video_collaboration_enabled') IS DISTINCT FROM 'boolean'
       OR (v_access_data -> 'project_role' <> 'null'::jsonb
           AND v_access_data ->> 'project_role' NOT IN ('annotator', 'reviewer', 'viewer')) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-target/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;

    IF v_access_data -> 'is_manager' = 'true'::jsonb
       OR (
           v_task.file_type = 'video'
           AND v_access_data -> 'video_collaboration_enabled' = 'true'::jsonb
       ) THEN
        v_visible := true;
    ELSIF v_task.batch_id IS NULL THEN
        v_visible := (
            (v_access_data ->> 'project_role' = 'annotator'
             AND v_task.assignee_id = p_actor_id)
            OR
            (v_access_data ->> 'project_role' = 'reviewer'
             AND v_task.reviewer_id = p_actor_id)
        );
    ELSE
        BEGIN
            SELECT batch.status, batch.annotator_id
            INTO v_batch_status, v_batch_annotator_id
            FROM public.task_batches AS batch
            WHERE batch.id = v_task.batch_id
            FOR SHARE NOWAIT;
        EXCEPTION
            WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-labeling-spatial-camera-target/v1',
                    'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'task_busy')
                );
        END;
        IF NOT FOUND THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-target/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'task_not_visible')
            );
        END IF;
        v_assigned := (v_task.assignee_id = p_actor_id)
            OR (v_task.assignee_id IS NULL AND v_batch_annotator_id = p_actor_id);
        IF v_access_data ->> 'project_role' = 'reviewer' THEN
            v_visible := v_batch_status IN ('active', 'annotating', 'reviewing');
        ELSE
            v_visible := v_batch_status IN ('active', 'annotating', 'rejected')
                OR (
                    v_access_data ->> 'project_role' = 'annotator'
                    AND (COALESCE(v_task.assignee_id, v_batch_annotator_id) = p_actor_id)
                    AND v_batch_status = 'reviewing'
                    AND v_task.status IN ('rejected', 'in_progress')
                );
            IF v_batch_status = 'rejected' AND NOT v_assigned THEN
                v_visible := false;
            END IF;
            IF v_task.assignee_id IS NOT NULL AND v_task.assignee_id <> p_actor_id THEN
                v_visible := false;
            END IF;
            IF v_batch_annotator_id IS NOT NULL AND NOT v_assigned THEN
                v_visible := false;
            END IF;
        END IF;
    END IF;
    IF NOT v_visible THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-target/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'task_not_visible')
        );
    END IF;

    SELECT link.dataset_item_id
    INTO v_link_item_id
    FROM public.task_dataset_item_links AS link
    WHERE link.task_id = p_task_id
      AND link.role = p_camera_role;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-target/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'camera_role_not_linked')
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-spatial-camera-target/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'task_id', p_task_id,
            'camera_role', p_camera_role,
            'dataset_item_id', v_link_item_id
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_spatial_camera_target(uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break

-- Final calibration writes must revalidate the task-role link, owner authority,
-- and cross-project sharing while the caller holds the Source item update lock.
CREATE FUNCTION aap_labeling_v1.lock_spatial_camera_authority(
    p_actor_id uuid,
    p_task_id uuid,
    p_camera_role text,
    p_dataset_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project_id uuid;
    v_manager jsonb;
    v_manager_data jsonb;
    v_link_item_id uuid;
    v_is_super_admin boolean;
    v_shared boolean;
BEGIN
    IF p_actor_id IS NULL OR p_task_id IS NULL OR p_dataset_item_id IS NULL
       OR p_camera_role IS NULL OR pg_catalog.char_length(p_camera_role) NOT BETWEEN 8 AND 50
       OR pg_catalog.left(p_camera_role, 7) <> 'camera_' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-authority/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    BEGIN
        SELECT task.project_id
        INTO v_project_id
        FROM public.tasks AS task
        WHERE task.id = p_task_id
        FOR SHARE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-authority/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'task_busy')
            );
    END;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-authority/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'task_missing')
        );
    END IF;

    v_manager := aap_workspace_v1.lock_project_manager(p_actor_id, v_project_id);
    IF v_manager ->> 'schema' IS DISTINCT FROM 'aap-project-manager/v1' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-authority/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    IF v_manager -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_manager #>> '{error,code}' IN ('account_not_active', 'account_busy') THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-authority/v1',
                'ok', false,
                'error', v_manager -> 'error'
            );
        ELSIF v_manager #>> '{error,code}' = 'project_missing' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-authority/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'project_missing')
            );
        ELSIF v_manager #>> '{error,code}' IN ('project_busy', 'membership_busy') THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-authority/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'task_busy')
            );
        ELSE
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-authority/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'project_manager_required')
            );
        END IF;
    END IF;
    v_manager_data := v_manager -> 'data';
    IF pg_catalog.jsonb_typeof(v_manager_data) IS DISTINCT FROM 'object'
       OR (v_manager_data - ARRAY[
            'actor_id', 'platform_role', 'project_id', 'owner_id',
            'data_type', 'scene_mode'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR v_manager_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_manager_data ->> 'project_id' IS DISTINCT FROM v_project_id::text
       OR v_manager_data ->> 'platform_role' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer')
       OR pg_catalog.jsonb_typeof(v_manager_data -> 'owner_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_manager_data -> 'data_type') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_manager_data -> 'scene_mode') IS DISTINCT FROM 'boolean' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-authority/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    v_is_super_admin := v_manager_data ->> 'platform_role' = 'super_admin';

    BEGIN
        SELECT link.dataset_item_id
        INTO v_link_item_id
        FROM public.task_dataset_item_links AS link
        WHERE link.task_id = p_task_id
          AND link.role = p_camera_role
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-authority/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'task_busy')
            );
    END;
    IF NOT FOUND OR v_link_item_id IS DISTINCT FROM p_dataset_item_id THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-authority/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'camera_role_not_linked')
        );
    END IF;

    BEGIN
        PERFORM link.task_id
        FROM public.task_dataset_item_links AS link
        JOIN public.tasks AS task ON task.id = link.task_id
        WHERE link.dataset_item_id = p_dataset_item_id
        ORDER BY link.task_id, link.role
        FOR SHARE OF link, task NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-camera-authority/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'task_busy')
            );
    END;
    SELECT EXISTS (
        SELECT 1
        FROM public.task_dataset_item_links AS link
        JOIN public.tasks AS task ON task.id = link.task_id
        WHERE link.dataset_item_id = p_dataset_item_id
          AND task.project_id <> v_project_id
    ) INTO v_shared;
    IF v_shared AND NOT v_is_super_admin THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-camera-authority/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'camera_shared_across_projects')
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-spatial-camera-authority/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'task_id', p_task_id,
            'camera_role', p_camera_role,
            'dataset_item_id', p_dataset_item_id,
            'project_id', v_project_id,
            'is_super_admin', v_is_super_admin,
            'shared_across_projects', v_shared
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_spatial_camera_authority(uuid, uuid, text, uuid) FROM PUBLIC;
-- aap-statement-break

-- Accepted point-cloud task creation retains its original authority. The read
-- helper deliberately does not lock so no transaction remains locked in S3 I/O.
CREATE FUNCTION aap_labeling_v1.read_spatial_preparation_scope(
    p_creation_operation_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation public.task_creation_operations%ROWTYPE;
    v_active boolean;
BEGIN
    IF p_creation_operation_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-preparation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT operation.*
    INTO v_operation
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_creation_operation_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-preparation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_missing')
        );
    END IF;
    IF v_operation.mode <> 'pointcloud' OR v_operation.state <> 'accepted' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-preparation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_cancelled')
        );
    END IF;
    v_active := aap_jobs_v1.task_creation_execution_active(
        v_operation.id, v_operation.job_id
    );
    IF v_active IS DISTINCT FROM true THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-preparation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_cancelled')
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-spatial-preparation/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'creation_operation_id', v_operation.id,
            'actor_id', v_operation.actor_id,
            'dataset_id', v_operation.dataset_id,
            'project_id', v_operation.project_id,
            'association_id', v_operation.association_id,
            'job_id', v_operation.job_id,
            'mode', v_operation.mode,
            'state', v_operation.state
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_spatial_preparation_scope(uuid) FROM PUBLIC;
-- aap-statement-break

-- Lock and revalidate the retained operation immediately before applying spatial
-- preparation, using the existing task-creation lock order and Job cancellation.
CREATE FUNCTION aap_labeling_v1.lock_spatial_preparation_scope(
    p_creation_operation_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation jsonb;
    v_data jsonb;
BEGIN
    IF p_creation_operation_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-preparation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    v_operation := aap_labeling_v1.read_dataset_task_creation(p_creation_operation_id);
    IF v_operation ->> 'schema' IS DISTINCT FROM 'aap-task-creation/v1' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-preparation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    IF v_operation -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_operation #>> '{error,code}' IN ('job_not_active', 'job_cancelled') THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-spatial-preparation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'operation_cancelled')
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-preparation/v1',
            'ok', false,
            'error', v_operation -> 'error'
        );
    END IF;
    v_data := v_operation -> 'data';
    IF v_data #>> '{mode}' IS DISTINCT FROM 'pointcloud'
       OR v_data #>> '{state}' IS DISTINCT FROM 'accepted' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-spatial-preparation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_cancelled')
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-spatial-preparation/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'creation_operation_id', v_data -> 'operation_id',
            'actor_id', v_data -> 'actor_id',
            'dataset_id', v_data -> 'dataset_id',
            'project_id', v_data -> 'project_id',
            'association_id', v_data -> 'association_id',
            'job_id', v_data -> 'job_id',
            'mode', v_data -> 'mode',
            'state', v_data -> 'state'
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_spatial_preparation_scope(uuid) FROM PUBLIC;
-- aap-statement-break

-- Audit uses its owner capability and preserves the existing public event shape.
CREATE FUNCTION aap_audit_v1.record_sensor_calibration_update(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_item_id uuid,
    p_task_id uuid,
    p_camera_role text,
    p_revision integer,
    p_digest text,
    p_context jsonb
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_audit_id bigint;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_item_id IS NULL
       OR p_task_id IS NULL OR p_camera_role IS NULL
       OR pg_catalog.char_length(p_camera_role) NOT BETWEEN 8 AND 50
       OR pg_catalog.left(p_camera_role, 7) <> 'camera_'
       OR p_revision IS NULL OR p_revision < 1
       OR p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$'
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid sensor calibration audit metadata';
    END IF;
    v_actor := aap_identity_v1.lock_audit_actor(p_actor_id);
    IF v_actor IS NULL
       OR pg_catalog.jsonb_typeof(v_actor) IS DISTINCT FROM 'object'
       OR (v_actor - ARRAY['schema', 'ok', 'data']) IS DISTINCT FROM '{}'::jsonb
       OR v_actor ->> 'schema' IS DISTINCT FROM 'aap-audit-actor/v1'
       OR v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_actor -> 'data') IS DISTINCT FROM 'object'
       OR ((v_actor -> 'data') - ARRAY['actor_id', 'email', 'platform_role']) IS DISTINCT FROM '{}'::jsonb
       OR v_actor #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,email}') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(v_actor #>> '{data,email}') NOT BETWEEN 1 AND 255
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,platform_role}') IS DISTINCT FROM 'string'
       OR v_actor #>> '{data,platform_role}' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'sensor calibration audit actor unavailable';
    END IF;
    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        p_actor_id, v_actor #>> '{data,email}', v_actor #>> '{data,platform_role}',
        'sensor_calibration.update', 'dataset_item', p_dataset_item_id::text,
        p_context ->> 'method', p_context ->> 'path', 200,
        NULLIF(p_context ->> 'ip', '')::pg_catalog.inet,
        p_context ->> 'request_id',
        pg_catalog.jsonb_build_object(
            'task_id', p_task_id,
            'camera_role', p_camera_role,
            'revision', p_revision,
            'digest', p_digest
        )
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_sensor_calibration_update(uuid, uuid, uuid, uuid, text, integer, text, jsonb) FROM PUBLIC;
