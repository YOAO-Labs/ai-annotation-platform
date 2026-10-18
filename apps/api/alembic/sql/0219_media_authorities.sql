-- Narrow owner projections for the Assets media control capabilities.
-- Assets receives these facts by EXECUTE only; it never reads Labeling,
-- Workspace, Identity, or Settings tables directly.

CREATE FUNCTION aap_workspace_v1.read_media_project_access(
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
    v_project_name text;
    v_video_collaboration jsonb;
    v_platform_role text;
    v_project_role text;
    v_is_manager boolean := false;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    SELECT facts.actor_id, facts.platform_role, facts.is_active
    INTO v_actor
    FROM aap_identity_v1.read_actor_facts(p_actor_id) AS facts;
    IF NOT FOUND OR v_actor.is_active IS DISTINCT FROM true THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'account_not_active')
        );
    END IF;
    IF v_actor.actor_id IS DISTINCT FROM p_actor_id
       OR v_actor.platform_role IS NULL
       OR v_actor.platform_role NOT IN (
            'super_admin', 'project_admin', 'employee', 'viewer'
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_account_role')
        );
    END IF;
    v_platform_role := v_actor.platform_role;

    SELECT project.owner_id, project.name, project.video_collaboration
    INTO v_owner_id, v_project_name, v_video_collaboration
    FROM public.projects AS project
    WHERE project.id = p_project_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'project_missing')
        );
    END IF;

    IF v_platform_role = 'super_admin'
       OR (v_platform_role = 'project_admin' AND v_owner_id = p_actor_id) THEN
        v_is_manager := true;
    ELSE
        SELECT membership.role
        INTO v_project_role
        FROM public.project_members AS membership
        WHERE membership.project_id = p_project_id
          AND membership.user_id = p_actor_id;
        IF NOT FOUND THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-workspace-media-access/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'project_missing')
            );
        END IF;
        IF v_project_role NOT IN ('annotator', 'reviewer', 'viewer')
           OR (v_platform_role = 'viewer' AND v_project_role <> 'viewer') THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-workspace-media-access/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_account_role')
            );
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-media-access/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'project_id', p_project_id,
            'platform_role', v_platform_role,
            'project_role', v_project_role,
            'is_manager', v_is_manager,
            'is_super_admin', v_platform_role = 'super_admin',
            'video_collaboration_enabled',
                COALESCE(v_video_collaboration -> 'enabled' = 'true'::jsonb, false),
            'project_name', v_project_name
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.read_media_project_access(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

-- Admission used only for an explicit task-scoped frame retry. Locks follow the
-- existing Identity -> Workspace project -> membership order. Task rows remain
-- unlocked so annotation writers do not gain a new media-specific NOWAIT edge.
CREATE FUNCTION aap_workspace_v1.lock_media_project_access(
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
    v_actor_data jsonb;
    v_platform_role text;
    v_owner_id uuid;
    v_project_name text;
    v_video_collaboration jsonb;
    v_project_role text;
    v_is_manager boolean := false;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor IS NULL
       OR v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1'
       OR pg_catalog.jsonb_typeof(v_actor -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-access/v1',
            'ok', false,
            'error', COALESCE(
                v_actor -> 'error',
                pg_catalog.jsonb_build_object('code', 'account_not_active')
            )
        );
    END IF;
    v_actor_data := v_actor -> 'data';
    IF pg_catalog.jsonb_typeof(v_actor_data) IS DISTINCT FROM 'object'
       OR (v_actor_data - ARRAY['actor_id', 'platform_role']) IS DISTINCT FROM '{}'::jsonb
       OR v_actor_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_actor_data -> 'platform_role') IS DISTINCT FROM 'string' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_account_role')
        );
    END IF;
    v_platform_role := v_actor_data ->> 'platform_role';
    IF v_platform_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_account_role')
        );
    END IF;

    BEGIN
        SELECT project.owner_id, project.name, project.video_collaboration
        INTO v_owner_id, v_project_name, v_video_collaboration
        FROM public.projects AS project
        WHERE project.id = p_project_id
        FOR SHARE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'task_busy')
        );
    END;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'project_missing')
        );
    END IF;

    IF v_platform_role = 'super_admin'
       OR (v_platform_role = 'project_admin' AND v_owner_id = p_actor_id) THEN
        v_is_manager := true;
    ELSE
        BEGIN
            SELECT membership.role
            INTO v_project_role
            FROM public.project_members AS membership
            WHERE membership.project_id = p_project_id
              AND membership.user_id = p_actor_id
            FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-workspace-media-access/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'task_busy')
            );
        END;
        IF NOT FOUND THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-workspace-media-access/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'project_missing')
            );
        END IF;
        IF v_project_role NOT IN ('annotator', 'reviewer', 'viewer')
           OR (v_platform_role = 'viewer' AND v_project_role <> 'viewer') THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-workspace-media-access/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_account_role')
            );
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-media-access/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'project_id', p_project_id,
            'platform_role', v_platform_role,
            'project_role', v_project_role,
            'is_manager', v_is_manager,
            'is_super_admin', v_platform_role = 'super_admin',
            'video_collaboration_enabled',
                COALESCE(v_video_collaboration -> 'enabled' = 'true'::jsonb, false),
            'project_name', v_project_name
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.lock_media_project_access(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_workspace_v1.read_media_managed_projects(
    p_actor_id uuid,
    p_project_ids uuid[]
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
    v_platform_role text;
    v_projects jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_project_ids IS NULL
       OR pg_catalog.array_position(p_project_ids, NULL) IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-manager-projects/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT facts.actor_id, facts.platform_role, facts.is_active
    INTO v_actor
    FROM aap_identity_v1.read_actor_facts(p_actor_id) AS facts;
    IF NOT FOUND OR v_actor.is_active IS DISTINCT FROM true THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-manager-projects/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'account_not_active')
        );
    END IF;
    IF v_actor.actor_id IS DISTINCT FROM p_actor_id
       OR v_actor.platform_role IS NULL
       OR v_actor.platform_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-media-manager-projects/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'permission_denied')
        );
    END IF;
    v_platform_role := v_actor.platform_role;
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'project_id', project.id,
                'project_name', project.name
            ) ORDER BY project.id
        ),
        '[]'::jsonb
    )
    INTO v_projects
    FROM (
        SELECT DISTINCT project.id, project.name
        FROM public.projects AS project
        WHERE project.id = ANY(p_project_ids)
          AND (
              v_platform_role = 'super_admin'
              OR project.owner_id = p_actor_id
          )
    ) AS project;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-media-manager-projects/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'platform_role', v_platform_role,
            'is_super_admin', v_platform_role = 'super_admin',
            'projects', v_projects
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.read_media_managed_projects(uuid, uuid[])
    FROM PUBLIC;
-- aap-statement-break

-- Current Labeling-owned task visibility facts. The read projection takes no
-- task locks; it composes Workspace's current project membership with the
-- canonical batch, collaboration, rework, assignee and reviewer rules.
CREATE FUNCTION aap_labeling_v1.read_media_task_access(
    p_actor_id uuid,
    p_task_id uuid,
    p_dataset_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_task record;
    v_access jsonb;
    v_access_data jsonb;
    v_role text;
    v_manager boolean;
    v_collaboration boolean;
    v_visible boolean := false;
    v_annotation_write boolean := false;
    v_assigned boolean;
BEGIN
    IF p_actor_id IS NULL OR p_task_id IS NULL OR p_dataset_item_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-task-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT
        task.id AS task_id,
        task.dataset_item_id,
        task.project_id,
        task.file_type,
        task.status AS task_status,
        task.batch_id,
        task.assignee_id,
        task.reviewer_id,
        batch.status AS batch_status,
        batch.annotator_id AS batch_annotator_id
    INTO v_task
    FROM public.tasks AS task
    LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
    WHERE task.id = p_task_id
      AND task.dataset_item_id = p_dataset_item_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-task-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'task_not_visible')
        );
    END IF;
    v_access := aap_workspace_v1.read_media_project_access(
        p_actor_id, v_task.project_id
    );
    IF v_access IS NULL
       OR v_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
       OR pg_catalog.jsonb_typeof(v_access -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-task-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_access #>> '{error,code}'
            WHEN 'account_not_active' THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-labeling-media-task-access/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'account_not_active')
                );
            WHEN 'account_busy' THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-labeling-media-task-access/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'account_busy')
                );
            WHEN 'task_busy' THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-labeling-media-task-access/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'task_busy')
                );
            WHEN 'project_missing' THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-labeling-media-task-access/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'task_not_visible')
                );
            ELSE
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-labeling-media-task-access/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'permission_denied')
                );
        END CASE;
    END IF;
    v_access_data := v_access -> 'data';
    IF pg_catalog.jsonb_typeof(v_access_data) IS DISTINCT FROM 'object'
       OR (v_access_data - ARRAY[
            'actor_id', 'project_id', 'platform_role', 'project_role', 'is_manager',
            'is_super_admin', 'video_collaboration_enabled', 'project_name'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR v_access_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_access_data ->> 'project_id' IS DISTINCT FROM v_task.project_id::text
       OR pg_catalog.jsonb_typeof(v_access_data -> 'platform_role') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_access_data -> 'is_manager') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_access_data -> 'is_super_admin') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_access_data -> 'video_collaboration_enabled') IS DISTINCT FROM 'boolean' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-task-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    v_role := v_access_data ->> 'project_role';
    v_manager := (v_access_data ->> 'is_manager')::boolean;
    v_collaboration := (v_access_data ->> 'video_collaboration_enabled')::boolean;

    IF v_manager OR (v_task.file_type = 'video' AND v_collaboration) THEN
        v_visible := true;
    ELSIF v_role = 'annotator' OR v_role = 'viewer' OR v_role = 'reviewer' THEN
        IF v_task.file_type = 'video' AND v_collaboration THEN
            v_visible := true;
        ELSIF v_task.batch_id IS NULL THEN
            v_visible := (v_role = 'annotator' AND v_task.assignee_id = p_actor_id)
                OR (v_role = 'reviewer' AND v_task.reviewer_id = p_actor_id);
        ELSIF v_role = 'reviewer' THEN
            v_visible := v_task.batch_status IN ('active', 'annotating', 'reviewing');
        ELSIF v_role IN ('annotator', 'viewer') THEN
            v_assigned := (
                (v_task.assignee_id IS NOT NULL AND v_task.assignee_id = p_actor_id)
                OR (v_task.assignee_id IS NULL
                    AND v_task.batch_annotator_id = p_actor_id)
            );
            v_visible := v_task.batch_status IN ('active', 'annotating', 'rejected');
            IF v_task.batch_status = 'reviewing' AND v_role = 'annotator'
               AND COALESCE(v_task.assignee_id, v_task.batch_annotator_id) = p_actor_id
               AND v_task.task_status IN ('rejected', 'in_progress') THEN
                v_visible := true;
            END IF;
            IF v_task.batch_status = 'rejected' AND NOT v_assigned THEN
                v_visible := false;
            END IF;
            IF v_task.assignee_id IS NOT NULL
               AND v_task.assignee_id <> p_actor_id THEN
                v_visible := false;
            END IF;
            IF v_task.batch_annotator_id IS NOT NULL AND NOT v_assigned THEN
                v_visible := false;
            END IF;
        END IF;
    END IF;
    IF NOT v_visible THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-task-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'task_not_visible')
        );
    END IF;

    v_annotation_write := v_manager OR v_role = 'annotator'
        OR (v_role = 'reviewer' AND v_task.task_status = 'review');
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-media-task-access/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'task_id', p_task_id,
            'dataset_item_id', p_dataset_item_id,
            'project_id', v_task.project_id,
            'file_type', v_task.file_type,
            'project_role', v_role,
            'is_manager', v_manager,
            'annotation_write', v_annotation_write
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_media_task_access(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

-- Resolve the first current visible video Task for the legacy item-only video
-- routes. This mirrors their task_access.assert_task_visible loop without
-- exposing Labeling tables to the Assets caller.
CREATE FUNCTION aap_labeling_v1.read_media_visible_video_item(
    p_actor_id uuid,
    p_dataset_item_id uuid
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
    v_task_id uuid;
    v_reply jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_item_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-task-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT facts.actor_id, facts.platform_role, facts.is_active
    INTO v_actor
    FROM aap_identity_v1.read_actor_facts(p_actor_id) AS facts;
    IF NOT FOUND OR v_actor.is_active IS DISTINCT FROM true THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-task-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'account_not_active')
        );
    END IF;
    IF v_actor.actor_id IS DISTINCT FROM p_actor_id
       OR v_actor.platform_role IS NULL
       OR v_actor.platform_role NOT IN (
            'super_admin', 'project_admin', 'employee', 'viewer'
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-task-access/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_account_role')
        );
    END IF;
    FOR v_task_id IN
        SELECT task.id
        FROM public.tasks AS task
        WHERE task.dataset_item_id = p_dataset_item_id
          AND task.file_type = 'video'
        ORDER BY task.id
    LOOP
        v_reply := aap_labeling_v1.read_media_task_access(
            p_actor_id, v_task_id, p_dataset_item_id
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-media-task-access/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-media-task-access/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
            );
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
           AND v_reply #>> '{error,code}' IN ('account_not_active', 'account_busy', 'task_busy', 'invalid_response') THEN
            RETURN v_reply;
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            CONTINUE;
        END IF;
        RETURN v_reply;
    END LOOP;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-media-task-access/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', 'task_not_visible')
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_media_visible_video_item(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

-- Same write capability as require_task_annotation_write, with project authority
-- held through the caller's acceptance transaction. It intentionally does not
-- lock Task or impose annotation mutation/editability/version checks.
CREATE FUNCTION aap_labeling_v1.lock_media_task_annotation_write(
    p_actor_id uuid,
    p_task_id uuid,
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
    v_task record;
    v_access jsonb;
    v_access_data jsonb;
    v_role text;
    v_manager boolean;
    v_can_write boolean;
    v_accumulated jsonb;
    v_review jsonb;
    v_review_round_id uuid;
    v_review_submitter_id uuid;
    v_effective_assignee_id uuid;
    v_entry jsonb;
    v_candidate uuid;
    v_accumulated_ids uuid[] := ARRAY[]::uuid[];
    v_review_ids uuid[] := ARRAY[]::uuid[];
    v_evidence_unknown boolean := false;
BEGIN
    IF p_actor_id IS NULL OR p_task_id IS NULL OR p_dataset_item_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-task-write/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT
        task.project_id,
        task.dataset_item_id,
        task.file_type,
        task.status AS task_status,
        task.batch_id,
        task.assignee_id,
        task.annotation_contributor_ids,
        task.review_round_id,
        task.review_contributor_ids,
        task.review_submitter_id,
        batch.annotator_id AS batch_annotator_id
    INTO v_task
    FROM public.tasks AS task
    LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
    WHERE task.id = p_task_id
      AND task.dataset_item_id = p_dataset_item_id;
    IF NOT FOUND OR v_task.file_type IS DISTINCT FROM 'video' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-task-write/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'task_not_visible')
        );
    END IF;
    v_access := aap_workspace_v1.lock_media_project_access(
        p_actor_id, v_task.project_id
    );
    IF v_access IS NULL
       OR v_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
       OR pg_catalog.jsonb_typeof(v_access -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-task-write/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_access #>> '{error,code}'
            WHEN 'account_not_active' THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-labeling-media-task-write/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'account_not_active')
                );
            WHEN 'account_busy' THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-labeling-media-task-write/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'account_busy')
                );
            WHEN 'task_busy' THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-labeling-media-task-write/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'task_busy')
                );
            ELSE
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-labeling-media-task-write/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'permission_denied')
                );
        END CASE;
    END IF;
    v_access_data := v_access -> 'data';
    IF pg_catalog.jsonb_typeof(v_access_data) IS DISTINCT FROM 'object'
       OR (v_access_data - ARRAY[
            'actor_id', 'project_id', 'platform_role', 'project_role', 'is_manager',
            'is_super_admin', 'video_collaboration_enabled', 'project_name'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR v_access_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_access_data ->> 'project_id' IS DISTINCT FROM v_task.project_id::text
       OR pg_catalog.jsonb_typeof(v_access_data -> 'is_manager') IS DISTINCT FROM 'boolean' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-task-write/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    v_role := v_access_data ->> 'project_role';
    v_manager := (v_access_data ->> 'is_manager')::boolean;
    v_can_write := v_manager OR v_role = 'annotator'
        OR (v_role = 'reviewer' AND v_task.task_status = 'review');
    IF NOT v_can_write THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-task-write/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'permission_denied')
        );
    END IF;

    IF v_task.task_status = 'review' AND (v_manager OR v_role = 'reviewer') THEN
        v_accumulated := v_task.annotation_contributor_ids;
        v_review := v_task.review_contributor_ids;
        v_review_round_id := v_task.review_round_id;
        v_review_submitter_id := v_task.review_submitter_id;
        v_effective_assignee_id := COALESCE(
            v_task.assignee_id, v_task.batch_annotator_id
        );
        IF v_review_round_id IS NULL
           OR pg_catalog.jsonb_typeof(v_accumulated) IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(v_review) IS DISTINCT FROM 'array'
           OR v_review_submitter_id IS NULL THEN
            v_evidence_unknown := true;
        ELSIF pg_catalog.jsonb_array_length(v_review) = 0 THEN
            v_evidence_unknown := true;
        ELSE
            FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(v_accumulated)
            LOOP
                IF pg_catalog.jsonb_typeof(v_entry) IS DISTINCT FROM 'string' THEN
                    v_evidence_unknown := true;
                    EXIT;
                END IF;
                BEGIN
                    v_candidate := (v_entry #>> '{}')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    v_evidence_unknown := true;
                    EXIT;
                END;
                v_accumulated_ids := pg_catalog.array_append(v_accumulated_ids, v_candidate);
            END LOOP;
            IF NOT v_evidence_unknown THEN
                FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(v_review)
                LOOP
                    IF pg_catalog.jsonb_typeof(v_entry) IS DISTINCT FROM 'string' THEN
                        v_evidence_unknown := true;
                        EXIT;
                    END IF;
                    BEGIN
                        v_candidate := (v_entry #>> '{}')::uuid;
                    EXCEPTION WHEN invalid_text_representation THEN
                        v_evidence_unknown := true;
                        EXIT;
                    END;
                    v_review_ids := pg_catalog.array_append(v_review_ids, v_candidate);
                END LOOP;
            END IF;
            IF NOT v_evidence_unknown AND (
                NOT (v_review_submitter_id = ANY(v_review_ids))
                OR EXISTS (
                    SELECT 1 FROM pg_catalog.unnest(v_accumulated_ids) AS ids(id)
                    WHERE NOT (ids.id = ANY(v_review_ids))
                )
            ) THEN
                v_evidence_unknown := true;
            END IF;
        END IF;
        IF v_evidence_unknown THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-media-task-write/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'task_busy')
            );
        END IF;
        IF p_actor_id = ANY(v_review_ids)
           OR p_actor_id = v_review_submitter_id
           OR (v_effective_assignee_id IS NOT NULL
               AND p_actor_id = v_effective_assignee_id) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-media-task-write/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'permission_denied')
            );
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-media-task-write/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'task_id', p_task_id,
            'dataset_item_id', p_dataset_item_id,
            'project_id', v_task.project_id,
            'annotation_write', true
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_media_task_annotation_write(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

-- The failure projection returns only one current Task context per requested
-- item. Project admins keep the legacy global policy for Dataset-only rows and
-- may see linked media only when a linked Task belongs to a project they own.
CREATE FUNCTION aap_labeling_v1.read_media_failure_context(
    p_actor_id uuid,
    p_dataset_item_ids uuid[]
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
    v_platform_role text;
    v_project_ids uuid[];
    v_manager_reply jsonb;
    v_manager_data jsonb;
    v_managed_projects jsonb;
    v_items jsonb := '[]'::jsonb;
    v_item_id uuid;
    v_task record;
    v_has_tasks boolean;
    v_is_super_admin boolean;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_item_ids IS NULL
       OR pg_catalog.array_position(p_dataset_item_ids, NULL) IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-failure-context/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT facts.actor_id, facts.platform_role, facts.is_active
    INTO v_actor
    FROM aap_identity_v1.read_actor_facts(p_actor_id) AS facts;
    IF NOT FOUND OR v_actor.is_active IS DISTINCT FROM true THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-failure-context/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'account_not_active')
        );
    END IF;
    IF v_actor.actor_id IS DISTINCT FROM p_actor_id
       OR v_actor.platform_role IS NULL
       OR v_actor.platform_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-failure-context/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'permission_denied')
        );
    END IF;
    v_platform_role := v_actor.platform_role;
    v_is_super_admin := v_platform_role = 'super_admin';

    SELECT COALESCE(pg_catalog.array_agg(DISTINCT task.project_id), ARRAY[]::uuid[])
    INTO v_project_ids
    FROM public.tasks AS task
    WHERE task.dataset_item_id = ANY(p_dataset_item_ids);
    v_manager_reply := aap_workspace_v1.read_media_managed_projects(
        p_actor_id, v_project_ids
    );
    IF v_manager_reply IS NULL
       OR v_manager_reply ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-manager-projects/v1'
       OR v_manager_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_manager_reply #> '{data,projects}') IS DISTINCT FROM 'array'
       OR v_manager_reply #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR v_manager_reply #>> '{data,platform_role}' IS DISTINCT FROM v_platform_role THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-media-failure-context/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    v_manager_data := v_manager_reply -> 'data';
    v_managed_projects := v_manager_data -> 'projects';

    FOR v_item_id IN
        SELECT DISTINCT item_id
        FROM pg_catalog.unnest(p_dataset_item_ids) AS requested(item_id)
        ORDER BY item_id
    LOOP
        SELECT EXISTS (
            SELECT 1 FROM public.tasks AS task
            WHERE task.dataset_item_id = v_item_id
        ) INTO v_has_tasks;
        SELECT
            task.id AS task_id,
            task.display_id AS task_display_id,
            task.project_id,
            project.project_name
        INTO v_task
        FROM public.tasks AS task
        JOIN pg_catalog.jsonb_to_recordset(v_managed_projects)
            AS project(project_id uuid, project_name text)
          ON project.project_id = task.project_id
        WHERE task.dataset_item_id = v_item_id
        ORDER BY task.id
        LIMIT 1;
        IF FOUND THEN
            v_items := v_items || pg_catalog.jsonb_build_array(
                pg_catalog.jsonb_build_object(
                    'dataset_item_id', v_item_id,
                    'task_id', v_task.task_id,
                    'task_display_id', v_task.task_display_id,
                    'project_id', v_task.project_id,
                    'project_name', v_task.project_name
                )
            );
        ELSIF NOT v_has_tasks THEN
            v_items := v_items || pg_catalog.jsonb_build_array(
                pg_catalog.jsonb_build_object(
                    'dataset_item_id', v_item_id,
                    'task_id', NULL,
                    'task_display_id', NULL,
                    'project_id', NULL,
                    'project_name', NULL
                )
            );
        END IF;
    END LOOP;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-media-failure-context/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'platform_role', v_platform_role,
            'is_super_admin', v_is_super_admin,
            'items', v_items
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_media_failure_context(uuid, uuid[])
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_settings_v1.read_video_media_preferences()
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_type text;
    v_value jsonb;
    v_lookahead integer;
BEGIN
    SELECT setting.value_type, setting.value_json
    INTO v_type, v_value
    FROM public.system_settings AS setting
    WHERE setting.key = 'video_chunk_warmup_lookahead';
    IF NOT FOUND OR v_value IS NULL OR pg_catalog.jsonb_typeof(v_value) = 'null' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-settings-video-media-preferences/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object('warmup_lookahead', NULL)
        );
    END IF;
    IF v_type IS DISTINCT FROM 'int'
       OR pg_catalog.jsonb_typeof(v_value) IS DISTINCT FROM 'number'
       OR (v_value #>> '{}') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.char_length(v_value #>> '{}') > 19
       OR (v_value #>> '{}')::numeric > 9223372036854775807::numeric THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-settings-video-media-preferences/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-settings-video-media-preferences/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'warmup_lookahead', (v_value #>> '{}')::bigint
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_settings_v1.read_video_media_preferences()
    FROM PUBLIC;
