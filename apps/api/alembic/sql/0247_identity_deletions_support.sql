-- Typed Workspace, Labeling and Audit capabilities for Identity account deletion.
CREATE SCHEMA aap_identity_deletions_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_deletions_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_deletion_operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    operation text NOT NULL CHECK (operation = 'delete_account'),
    target_user_id uuid NOT NULL,
    transfer_to_user_id uuid,
    request_fingerprint text NOT NULL CHECK (
        request_fingerprint COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    derivation_id text NOT NULL CHECK (
        derivation_id COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    target_generation bigint NOT NULL CHECK (target_generation >= 0),
    target_is_active boolean NOT NULL,
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    audit_id bigint,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE INDEX ix_identity_deletion_operation_receipts_actor
    ON public.identity_deletion_operation_receipts (actor_id, created_at DESC);
-- aap-statement-break
COMMENT ON TABLE public.identity_deletion_operation_receipts IS
    'Frozen soft-deletion results and post-operation lifecycle fences; no User FK, credentials, or bearer copies.';
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_deletion_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.lock_identity_deletion_scope(
    p_actor_id uuid,
    p_target_id uuid
)
RETURNS TABLE(has_projects boolean, all_projects_owned boolean)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_has_projects boolean;
    v_all_projects_owned boolean;
BEGIN
    IF p_actor_id IS NULL OR p_target_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity deletion Workspace scope request';
    END IF;

    BEGIN
        SELECT scope.has_projects, scope.all_projects_owned
        INTO STRICT v_has_projects, v_all_projects_owned
        FROM aap_workspace_v1.lock_identity_suspension_scope(
            p_actor_id, p_target_id, 'suspend_account'
        ) AS scope;
    EXCEPTION WHEN no_data_found OR too_many_rows THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity deletion Workspace scope result';
    END;

    IF v_has_projects IS NULL OR v_all_projects_owned IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity deletion Workspace scope result';
    END IF;
    RETURN QUERY SELECT v_has_projects, v_all_projects_owned;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.lock_identity_deletion_scope(
    uuid, uuid
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.identity_deletion_transfer_covered(
    p_receiver_id uuid,
    p_pending_project_ids uuid[]
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_covered boolean;
BEGIN
    IF p_receiver_id IS NULL OR p_pending_project_ids IS NULL
       OR pg_catalog.array_position(p_pending_project_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity deletion transfer scope request';
    END IF;

    SELECT NOT EXISTS (
        SELECT 1
        FROM (
            SELECT DISTINCT projects.project_id
            FROM pg_catalog.unnest(p_pending_project_ids) AS projects(project_id)
        ) AS pending_project
        WHERE NOT EXISTS (
            SELECT 1
            FROM public.project_members AS member
            WHERE member.user_id = p_receiver_id
              AND member.project_id = pending_project.project_id
        )
    )
    INTO v_covered;
    IF v_covered IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity deletion transfer scope result';
    END IF;
    RETURN v_covered;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.identity_deletion_transfer_covered(
    uuid, uuid[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.identity_deletion_work(
    p_target_user_id uuid
)
RETURNS TABLE(
    pending_task_count bigint,
    locked_task_count bigint,
    sample_task_ids uuid[],
    pending_project_ids uuid[]
)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_pending_task_count bigint;
    v_locked_task_count bigint;
    v_sample_task_ids uuid[];
    v_pending_project_ids uuid[];
BEGIN
    IF p_target_user_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity deletion Labeling work request';
    END IF;

    SELECT pg_catalog.count(*)::bigint
    INTO v_pending_task_count
    FROM public.tasks AS task
    WHERE task.assignee_id = p_target_user_id
      AND task.status IN ('pending', 'in_progress', 'review', 'rejected');

    SELECT COALESCE(
               pg_catalog.array_agg(sample.task_id ORDER BY sample.created_at DESC),
               ARRAY[]::uuid[]
           )
    INTO v_sample_task_ids
    FROM (
        SELECT task.id AS task_id, task.created_at
        FROM public.tasks AS task
        WHERE task.assignee_id = p_target_user_id
          AND task.status IN ('pending', 'in_progress', 'review', 'rejected')
        ORDER BY task.created_at DESC
        LIMIT 5
    ) AS sample;

    SELECT pg_catalog.count(*)::bigint
    INTO v_locked_task_count
    FROM public.task_locks AS task_lock
    WHERE task_lock.user_id = p_target_user_id;

    SELECT COALESCE(
               pg_catalog.array_agg(project_refs.project_id ORDER BY project_refs.project_id),
               ARRAY[]::uuid[]
           )
    INTO v_pending_project_ids
    FROM (
        SELECT DISTINCT task.project_id
        FROM public.tasks AS task
        WHERE task.assignee_id = p_target_user_id
          AND task.status IN ('pending', 'in_progress', 'review', 'rejected')
          AND task.project_id IS NOT NULL
    ) AS project_refs;

    RETURN QUERY SELECT
        v_pending_task_count,
        v_locked_task_count,
        v_sample_task_ids,
        v_pending_project_ids;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.identity_deletion_work(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.transfer_identity_deletion_work(
    p_from_user_id uuid,
    p_to_user_id uuid
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_transferred_count bigint;
BEGIN
    IF p_from_user_id IS NULL OR p_to_user_id IS NULL
       OR p_from_user_id = p_to_user_id THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity deletion Labeling transfer request';
    END IF;

    UPDATE public.tasks AS task
    SET assignee_id = p_to_user_id,
        assigned_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE task.assignee_id = p_from_user_id
      AND task.status IN ('pending', 'in_progress', 'review', 'rejected');
    GET DIAGNOSTICS v_transferred_count = ROW_COUNT;

    DELETE FROM public.task_locks AS task_lock
    WHERE task_lock.user_id = p_from_user_id;
    RETURN v_transferred_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.transfer_identity_deletion_work(
    uuid, uuid
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_identity_deletion_event(
    p_actor jsonb,
    p_target_user_id uuid,
    p_detail jsonb,
    p_context jsonb
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
    v_field_count integer;
    v_actor_id uuid;
    v_actor_email text;
    v_actor_role text;
    v_ip inet;
    v_transferred_count bigint;
    v_released_locks bigint;
    v_redacted_rows bigint;
    v_audit_id bigint;
    v_previous_update_setting text;
    v_audit_detail jsonb;
BEGIN
    IF p_target_user_id IS NULL
       OR pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object'
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity deletion Audit request';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_context);
    IF v_field_count <> 4
       OR NOT (p_context ?& ARRAY['method', 'path', 'ip', 'request_id'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_context) AS field(name)
           WHERE field.name NOT IN ('method', 'path', 'ip', 'request_id')
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity deletion Audit context';
    END IF;
    v_ip := NULLIF(p_context->>'ip', '')::pg_catalog.inet;

    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity deletion Audit actor';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_actor);
    IF v_field_count <> 3
       OR NOT (p_actor ?& ARRAY['actor_id', 'email', 'role'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_actor) AS field(name)
           WHERE field.name NOT IN ('actor_id', 'email', 'role')
       ) OR pg_catalog.jsonb_typeof(p_actor->'actor_id') IS DISTINCT FROM 'string'
       OR (p_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_actor->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_actor->>'email') NOT BETWEEN 1 AND 255
       OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
       OR p_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity deletion Audit actor';
    END IF;
    v_actor_id := (p_actor->>'actor_id')::uuid;
    IF v_actor_id = p_target_user_id THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Identity deletion Audit actor cannot equal target';
    END IF;
    v_actor_email := p_actor->>'email';
    v_actor_role := p_actor->>'role';

    IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity deletion Audit detail';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_detail);
    IF v_field_count <> 5
       OR NOT (p_detail ?& ARRAY[
           'email', 'role', 'transferred_to', 'transferred_count', 'released_locks'
       ])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_detail) AS field(name)
           WHERE field.name NOT IN (
               'email', 'role', 'transferred_to', 'transferred_count', 'released_locks'
           )
       )
       OR pg_catalog.jsonb_typeof(p_detail->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_detail->>'email') > 255
       OR pg_catalog.jsonb_typeof(p_detail->'role') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_detail->>'role') > 50
       OR pg_catalog.jsonb_typeof(p_detail->'transferred_to') NOT IN ('string', 'null')
       OR (pg_catalog.jsonb_typeof(p_detail->'transferred_to') = 'string'
           AND (p_detail->>'transferred_to') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
       OR pg_catalog.jsonb_typeof(p_detail->'transferred_count') IS DISTINCT FROM 'number'
       OR (p_detail->>'transferred_count') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(p_detail->'released_locks') IS DISTINCT FROM 'number'
       OR (p_detail->>'released_locks') !~ '^(0|[1-9][0-9]*)$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity deletion Audit detail';
    END IF;
    BEGIN
        v_transferred_count := (p_detail->>'transferred_count')::bigint;
        v_released_locks := (p_detail->>'released_locks')::bigint;
    EXCEPTION WHEN numeric_value_out_of_range THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity deletion Audit counts';
    END;

    v_previous_update_setting :=
        pg_catalog.current_setting('app.allow_audit_update', true);
    PERFORM pg_catalog.set_config('app.allow_audit_update', 'true', true);
    UPDATE public.audit_logs AS audit
    SET actor_email = NULL,
        actor_role = NULL
    WHERE audit.actor_id = p_target_user_id;
    GET DIAGNOSTICS v_redacted_rows = ROW_COUNT;
    IF v_redacted_rows > 0 THEN
        v_audit_detail := p_detail || pg_catalog.jsonb_build_object(
            'redacted_audit_rows', v_redacted_rows
        );
    ELSE
        v_audit_detail := p_detail;
    END IF;
    PERFORM pg_catalog.set_config(
        'app.allow_audit_update', COALESCE(v_previous_update_setting, ''), true
    );

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        v_actor_id, v_actor_email, v_actor_role, 'user.delete', 'user',
        p_target_user_id::text, p_context->>'method', p_context->>'path', 200,
        v_ip, p_context->>'request_id', v_audit_detail
    ) RETURNING id INTO v_audit_id;

    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_identity_deletion_event(
    jsonb, uuid, jsonb, jsonb
) FROM PUBLIC;
