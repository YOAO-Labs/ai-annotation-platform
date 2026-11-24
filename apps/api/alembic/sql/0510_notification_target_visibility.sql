CREATE OR REPLACE FUNCTION yoao_diagnostics_v1.bug_report_visible_to_actor(
    p_actor uuid,
    p_report uuid
)
RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_role text := yoao_diagnostics_v1._actor_role(p_actor, false);
    v_reporter_id uuid;
BEGIN
    IF p_actor IS NULL OR p_report IS NULL OR v_role IS NULL THEN
        RETURN false;
    END IF;
    SELECT b.reporter_id INTO v_reporter_id
    FROM public.bug_reports AS b
    WHERE b.id = p_report;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    RETURN v_role IN ('super_admin', 'project_admin') OR v_reporter_id = p_actor;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_diagnostics_v1.bug_report_visible_to_actor(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1._notification_task_visible(
    p_actor uuid,
    p_actor_role text,
    p_task_id uuid,
    p_expected_project_id uuid
)
RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_task record;
    v_project jsonb;
    v_owner_id uuid;
    v_member_role text;
    v_batch record;
    v_is_assigned boolean;
BEGIN
    SELECT t.id, t.project_id, t.batch_id, t.file_type, t.status, t.assignee_id, t.reviewer_id
    INTO v_task
    FROM public.tasks AS t
    WHERE t.id = p_task_id;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF p_expected_project_id IS NOT NULL AND v_task.project_id <> p_expected_project_id THEN
        RETURN false;
    END IF;

    v_project := yoao_workspace_v1.read_project_row(v_task.project_id);
    IF v_project IS NULL THEN
        RETURN false;
    END IF;
    v_owner_id := CASE
        WHEN NULLIF(v_project ->> 'owner_id', '') IS NOT NULL
            THEN (v_project ->> 'owner_id')::uuid
        ELSE NULL
    END;
    IF p_actor_role = 'super_admin'
       OR (p_actor_role = 'project_admin' AND v_owner_id = p_actor) THEN
        RETURN true;
    END IF;

    SELECT m.role INTO v_member_role
    FROM yoao_workspace_v1.project_member_facts AS m
    WHERE m.project_id = v_task.project_id
      AND m.user_id = p_actor;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF p_actor_role = 'viewer' THEN
        v_member_role := 'viewer';
    END IF;

    IF v_task.file_type = 'video'
       AND COALESCE((v_project -> 'video_collaboration' ->> 'enabled')::boolean, false) IS TRUE THEN
        RETURN true;
    END IF;

    IF v_task.batch_id IS NULL THEN
        RETURN (v_member_role = 'annotator' AND v_task.assignee_id = p_actor)
            OR (v_member_role = 'reviewer' AND v_task.reviewer_id = p_actor);
    END IF;

    SELECT b.id, b.status, b.annotator_id
    INTO v_batch
    FROM public.task_batches AS b
    WHERE b.id = v_task.batch_id;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF v_member_role = 'reviewer' THEN
        RETURN v_batch.status IN ('active', 'annotating', 'reviewing');
    END IF;

    v_is_assigned := (v_task.assignee_id = p_actor)
        OR (v_task.assignee_id IS NULL AND v_batch.annotator_id = p_actor);

    IF v_member_role = 'annotator'
       AND v_batch.status = 'reviewing'
       AND v_task.status IN ('rejected', 'in_progress')
       AND COALESCE(v_task.assignee_id, v_batch.annotator_id) = p_actor THEN
        RETURN true;
    END IF;

    IF v_batch.status NOT IN ('active', 'annotating', 'rejected') THEN
        RETURN false;
    END IF;
    IF v_batch.status = 'rejected' AND NOT v_is_assigned THEN
        RETURN false;
    END IF;
    IF v_task.assignee_id IS NOT NULL AND v_task.assignee_id <> p_actor THEN
        RETURN false;
    END IF;
    IF v_batch.annotator_id IS NOT NULL AND NOT v_is_assigned THEN
        RETURN false;
    END IF;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1._notification_task_visible(
    uuid, text, uuid, uuid
) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1.notification_target_visible_to_actor(
    p_actor uuid,
    p_target_type text,
    p_target_id uuid,
    p_project_id uuid,
    p_task_id uuid
)
RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor record;
    v_project jsonb;
    v_owner_id uuid;
    v_project_id uuid;
    v_task_id uuid;
    v_is_manager boolean;
    v_is_member boolean;
BEGIN
    IF p_actor IS NULL OR p_target_type IS NULL OR p_target_id IS NULL THEN
        RETURN false;
    END IF;

    SELECT a.actor_id, a.platform_role, a.is_active
    INTO v_actor
    FROM yoao_identity_v1.read_actor_facts(p_actor) AS a;
    IF NOT FOUND OR COALESCE(v_actor.is_active, false) IS NOT TRUE THEN
        RETURN false;
    END IF;

    IF p_target_type = 'task' THEN
        RETURN yoao_labeling_v1._notification_task_visible(
            p_actor, v_actor.platform_role, p_target_id, p_project_id
        );

    ELSIF p_target_type = 'batch' THEN
        SELECT b.project_id INTO v_project_id
        FROM public.task_batches AS b
        WHERE b.id = p_target_id;
        IF NOT FOUND OR (p_project_id IS NOT NULL AND v_project_id <> p_project_id) THEN
            RETURN false;
        END IF;

    ELSIF p_target_type = 'feedback' THEN
        SELECT f.project_id, f.task_id INTO v_project_id, v_task_id
        FROM public.annotation_feedbacks AS f
        WHERE f.id = p_target_id AND f.is_active IS TRUE;
        IF NOT FOUND OR (p_project_id IS NOT NULL AND v_project_id <> p_project_id) THEN
            RETURN false;
        END IF;
        IF v_task_id IS NOT NULL THEN
            RETURN yoao_labeling_v1._notification_task_visible(
                p_actor, v_actor.platform_role, v_task_id, v_project_id
            );
        END IF;

    ELSIF p_target_type = 'annotation_comment' THEN
        SELECT c.project_id, a.task_id INTO v_project_id, v_task_id
        FROM public.annotation_comments AS c
        JOIN public.annotations AS a
          ON a.id = c.annotation_id
         AND a.is_active IS TRUE
         AND a.was_cancelled IS FALSE
        WHERE c.id = p_target_id AND c.is_active IS TRUE;
        IF NOT FOUND OR (p_project_id IS NOT NULL AND v_project_id <> p_project_id) THEN
            RETURN false;
        END IF;
        RETURN yoao_labeling_v1._notification_task_visible(
            p_actor, v_actor.platform_role, v_task_id, v_project_id
        );

    ELSIF p_target_type = 'failed_prediction' THEN
        SELECT fp.project_id INTO v_project_id
        FROM public.failed_predictions AS fp
        WHERE fp.id = p_target_id;
        IF NOT FOUND OR (p_project_id IS NOT NULL AND v_project_id <> p_project_id) THEN
            RETURN false;
        END IF;
        v_project := yoao_workspace_v1.read_project_row(v_project_id);
        IF v_project IS NULL THEN
            RETURN false;
        END IF;
        v_owner_id := CASE
            WHEN NULLIF(v_project ->> 'owner_id', '') IS NOT NULL
                THEN (v_project ->> 'owner_id')::uuid
            ELSE NULL
        END;
        RETURN v_actor.platform_role = 'super_admin'
            OR (v_actor.platform_role = 'project_admin' AND v_owner_id = p_actor);

    ELSE
        RETURN false;
    END IF;

    v_project := yoao_workspace_v1.read_project_row(v_project_id);
    IF v_project IS NULL THEN
        RETURN false;
    END IF;
    v_owner_id := CASE
        WHEN NULLIF(v_project ->> 'owner_id', '') IS NOT NULL
            THEN (v_project ->> 'owner_id')::uuid
        ELSE NULL
    END;
    v_is_manager := v_actor.platform_role = 'super_admin'
        OR (v_actor.platform_role = 'project_admin' AND v_owner_id = p_actor);
    IF v_is_manager THEN
        RETURN true;
    END IF;
    SELECT EXISTS (
        SELECT 1 FROM yoao_workspace_v1.project_member_facts AS m
        WHERE m.project_id = v_project_id AND m.user_id = p_actor
    ) INTO v_is_member;
    RETURN v_is_member;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.notification_target_visible_to_actor(
    uuid, text, uuid, uuid, uuid
) FROM PUBLIC;
