-- Narrow Workspace, Labeling and Audit facts for platform-role changes.
CREATE SCHEMA aap_identity_platform_roles_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_platform_roles_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_platform_role_operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    target_user_id uuid NOT NULL,
    operation text NOT NULL CHECK (operation = 'change_platform_role'),
    request_fingerprint text NOT NULL CHECK (
        request_fingerprint COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    derivation_id text NOT NULL CHECK (
        derivation_id COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    audit_id bigint,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE INDEX ix_identity_platform_role_operation_receipts_actor
    ON public.identity_platform_role_operation_receipts (actor_id, created_at DESC);
-- aap-statement-break
COMMENT ON TABLE public.identity_platform_role_operation_receipts IS
    'Frozen accepted platform-role change results; no User FK, credentials, or bearer copies.';
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_platform_role_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.identity_platform_role_facts(
    p_target_id uuid
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-identity-platform-role-facts/v1',
        'target_user_id', p_target_id,
        'owned_project_count', (
            SELECT pg_catalog.count(*)::bigint
            FROM public.projects AS project
            WHERE project.owner_id = p_target_id
        ),
        'non_viewer_membership_count', (
            SELECT pg_catalog.count(*)::bigint
            FROM public.project_members AS member
            WHERE member.user_id = p_target_id
              AND member.role <> 'viewer'
        ),
        'membership_projects', COALESCE(
            (
                SELECT pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'project_id', project.id,
                        'project_name', project.name,
                        'membership_role', member.role
                    ) ORDER BY project.name ASC, project.id
                )
                FROM public.project_members AS member
                JOIN public.projects AS project ON project.id = member.project_id
                WHERE member.user_id = p_target_id
            ),
            '[]'::jsonb
        )
    )
    WHERE p_target_id IS NOT NULL
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.identity_platform_role_facts(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.lock_identity_platform_role_owned_projects(
    p_target_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_target_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid platform-role owned-project lock request';
    END IF;
    PERFORM project.id
    FROM public.projects AS project
    WHERE project.owner_id = p_target_id
    ORDER BY project.id
    FOR UPDATE OF project NOWAIT;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.lock_identity_platform_role_owned_projects(
    uuid
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.identity_platform_role_facts(
    p_target_id uuid,
    p_membership_project_ids uuid[]
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_unfinished_effective_work_count bigint;
    v_membership_projects jsonb;
    v_project_count bigint;
    v_unique_project_count bigint;
BEGIN
    IF p_target_id IS NULL OR p_membership_project_ids IS NULL
       OR pg_catalog.array_position(p_membership_project_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid platform-role Labeling facts request';
    END IF;
    SELECT pg_catalog.cardinality(p_membership_project_ids)::bigint,
           pg_catalog.count(DISTINCT project_id)::bigint
    INTO v_project_count, v_unique_project_count
    FROM pg_catalog.unnest(p_membership_project_ids) AS ids(project_id);
    IF v_project_count IS DISTINCT FROM v_unique_project_count THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'duplicate platform-role membership project';
    END IF;

    SELECT pg_catalog.count(task.id)::bigint
    INTO v_unfinished_effective_work_count
    FROM public.tasks AS task
    LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
    WHERE task.status IN ('pending', 'in_progress', 'rejected', 'review')
      AND (
          COALESCE(task.assignee_id, batch.annotator_id) = p_target_id
          OR COALESCE(task.reviewer_id, batch.reviewer_id) = p_target_id
      );

    WITH memberships AS (
        SELECT ids.project_id, ids.ordinality
        FROM pg_catalog.unnest(p_membership_project_ids)
            WITH ORDINALITY AS ids(project_id, ordinality)
    ),
    batch_counts AS (
        SELECT batch.project_id,
               pg_catalog.count(*) FILTER (
                   WHERE batch.annotator_id = p_target_id
               )::bigint AS annotator_batch_count,
               pg_catalog.count(*) FILTER (
                   WHERE batch.reviewer_id = p_target_id
               )::bigint AS reviewer_batch_count
        FROM public.task_batches AS batch
        WHERE batch.project_id = ANY(p_membership_project_ids)
        GROUP BY batch.project_id
    ),
    task_counts AS (
        SELECT task.project_id,
               pg_catalog.count(*) FILTER (
                   WHERE task.assignee_id = p_target_id
               )::bigint AS assigned_task_count,
               pg_catalog.count(*) FILTER (
                   WHERE task.reviewer_id = p_target_id
               )::bigint AS review_task_count
        FROM public.tasks AS task
        WHERE task.project_id = ANY(p_membership_project_ids)
        GROUP BY task.project_id
    )
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'project_id', memberships.project_id,
                'annotator_batch_count', COALESCE(
                    batch_counts.annotator_batch_count, 0::bigint
                ),
                'reviewer_batch_count', COALESCE(
                    batch_counts.reviewer_batch_count, 0::bigint
                ),
                'assigned_task_count', COALESCE(
                    task_counts.assigned_task_count, 0::bigint
                ),
                'review_task_count', COALESCE(
                    task_counts.review_task_count, 0::bigint
                )
            ) ORDER BY memberships.ordinality
        ),
        '[]'::jsonb
    )
    INTO v_membership_projects
    FROM memberships
    LEFT JOIN batch_counts USING (project_id)
    LEFT JOIN task_counts USING (project_id);

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-identity-platform-role-facts/v1',
        'target_user_id', p_target_id,
        'unfinished_effective_work_count', v_unfinished_effective_work_count,
        'membership_projects', v_membership_projects
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.identity_platform_role_facts(
    uuid, uuid[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_identity_platform_role_change(
    p_actor jsonb,
    p_target_user_id uuid,
    p_context jsonb,
    p_email text,
    p_old_role text,
    p_new_role text
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_count integer;
    v_actor_id uuid;
    v_actor_email text;
    v_actor_role text;
    v_ip inet;
    v_audit_id bigint;
BEGIN
    IF p_target_user_id IS NULL OR p_email IS NULL OR p_old_role IS NULL
       OR p_new_role IS NULL
       OR pg_catalog.char_length(p_email) > 255
       OR pg_catalog.char_length(p_old_role) > 50
       OR p_new_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer')
       OR pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object'
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity platform-role Audit facts';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_context);
    IF v_count <> 4 OR NOT (p_context ?& ARRAY[
        'method', 'path', 'ip', 'request_id'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_context) AS field(name)
        WHERE field.name NOT IN ('method', 'path', 'ip', 'request_id')
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity platform-role Audit context';
    END IF;
    v_ip := NULLIF(p_context->>'ip', '')::pg_catalog.inet;

    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity platform-role Audit actor';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_actor);
    IF v_count <> 3 OR NOT (p_actor ?& ARRAY['actor_id', 'email', 'role'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_actor) AS field(name)
           WHERE field.name NOT IN ('actor_id', 'email', 'role')
       ) OR pg_catalog.jsonb_typeof(p_actor->'actor_id') IS DISTINCT FROM 'string'
       OR (p_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_actor->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_actor->>'email') > 255
       OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
       OR p_actor->>'role' IS DISTINCT FROM 'super_admin' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity platform-role Audit actor';
    END IF;
    v_actor_id := (p_actor->>'actor_id')::uuid;
    v_actor_email := p_actor->>'email';
    v_actor_role := p_actor->>'role';

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        v_actor_id, v_actor_email, v_actor_role,
        'user.role_change', 'user', p_target_user_id::text,
        p_context->>'method', p_context->>'path', 200,
        v_ip, p_context->>'request_id',
        pg_catalog.jsonb_build_object(
            'email', p_email,
            'old', p_old_role,
            'new', p_new_role
        )
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_identity_platform_role_change(
    jsonb, uuid, jsonb, text, text, text
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_platform_roles_v1._preview_snapshot(
    p_target_id uuid
)
RETURNS TABLE(
    user_record jsonb,
    workspace_facts jsonb,
    labeling_facts jsonb,
    other_unreserved_super_admins bigint
)
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT aap_identity_directory_v1._directory_user(account.id),
           workspace.facts,
           aap_labeling_v1.identity_platform_role_facts(
               account.id,
               ARRAY(
                   SELECT (member.value->>'project_id')::uuid
                   FROM pg_catalog.jsonb_array_elements(
                       workspace.facts->'membership_projects'
                   ) WITH ORDINALITY AS member(value, ordinality)
                   ORDER BY member.ordinality
               )
           ),
           (
               SELECT pg_catalog.count(*)::bigint
               FROM public.users AS other
               WHERE other.role = 'super_admin'
                 AND other.id <> account.id
                 AND other.is_active IS TRUE
                 AND other.deactivation_requested_at IS NULL
                 AND other.deactivation_scheduled_at IS NULL
           )
    FROM public.users AS account
    CROSS JOIN LATERAL (
        SELECT aap_workspace_v1.identity_platform_role_facts(account.id) AS facts
    ) AS workspace
    WHERE account.id = p_target_id
      AND account.is_active IS TRUE
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_platform_roles_v1._preview_snapshot(uuid)
    FROM PUBLIC;
