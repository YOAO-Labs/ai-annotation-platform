-- Narrow Workspace, Labeling and Audit capabilities for account offboarding.
CREATE SCHEMA aap_identity_offboarding_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_offboarding_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_offboarding_operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    operation text NOT NULL CHECK (operation = 'commit_offboarding'),
    target_user_id uuid NOT NULL,
    request_fingerprint text NOT NULL CHECK (
        request_fingerprint COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    derivation_id text NOT NULL CHECK (
        derivation_id COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    target_generation bigint NOT NULL CHECK (target_generation >= 1),
    target_is_active boolean NOT NULL CHECK (target_is_active IS FALSE),
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    scope_basis jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(scope_basis) = 'object'),
    audit_id bigint NOT NULL CHECK (audit_id > 0),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE INDEX ix_identity_offboarding_operation_receipts_actor
    ON public.identity_offboarding_operation_receipts (actor_id, created_at DESC);
-- aap-statement-break
COMMENT ON TABLE public.identity_offboarding_operation_receipts IS
    'Frozen private offboarding replies and accepted authority scope; no User FK, credential, token, raw preview version or raw reason.';
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_offboarding_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.lock_identity_offboarding_scope(
    p_target_id uuid,
    p_payload_project_ids uuid[],
    p_receiver_ids uuid[]
)
RETURNS uuid[]
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project_ids uuid[];
    v_locked_project_ids uuid[] := ARRAY[]::uuid[];
    v_project_id uuid;
    v_member_id uuid;
BEGIN
    IF p_target_id IS NULL OR p_payload_project_ids IS NULL OR p_receiver_ids IS NULL
       OR pg_catalog.array_position(p_payload_project_ids, NULL::uuid) IS NOT NULL
       OR pg_catalog.array_position(p_receiver_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding Workspace scope request';
    END IF;

    WITH derived AS MATERIALIZED (
        SELECT project.id AS project_id
        FROM public.projects AS project
        WHERE project.owner_id = p_target_id
        UNION
        SELECT member.project_id
        FROM public.project_members AS member
        WHERE member.user_id = p_target_id
        UNION
        SELECT refs.project_id
        FROM aap_labeling_v1.identity_directory_project_refs(
            ARRAY[p_target_id]::uuid[]
        ) AS refs
        WHERE refs.user_id = p_target_id
        UNION
        SELECT requested.project_id
        FROM pg_catalog.unnest(p_payload_project_ids) AS requested(project_id)
    )
    SELECT COALESCE(
               pg_catalog.array_agg(project.id ORDER BY project.id),
               ARRAY[]::uuid[]
           )
    INTO v_project_ids
    FROM derived
    JOIN public.projects AS project ON project.id = derived.project_id;

    -- Preserve the legacy Project -> Member lock order and the payload's extra
    -- project locks. Missing payload projects are ignored as before.
    FOR v_project_id IN
        SELECT project.id
        FROM public.projects AS project
        WHERE project.id = ANY(v_project_ids)
        ORDER BY project.id
        FOR UPDATE OF project NOWAIT
    LOOP
        v_locked_project_ids := pg_catalog.array_append(
            v_locked_project_ids, v_project_id
        );
    END LOOP;

    FOR v_member_id IN
        SELECT member.id
        FROM public.project_members AS member
        WHERE member.project_id = ANY(v_locked_project_ids)
          AND member.user_id = ANY(ARRAY[p_target_id]::uuid[] || p_receiver_ids)
        ORDER BY member.project_id, member.user_id
        FOR UPDATE OF member NOWAIT
    LOOP
        NULL;
    END LOOP;
    RETURN v_locked_project_ids;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.lock_identity_offboarding_scope(
    uuid, uuid[], uuid[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.identity_offboarding_replay_scope(
    p_actor_id uuid,
    p_project_ids uuid[]
)
RETURNS TABLE(projects_exist boolean, all_projects_owned boolean)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project_count bigint;
    v_unique_project_count bigint;
    v_existing_count bigint;
    v_owned_count bigint;
BEGIN
    IF p_actor_id IS NULL OR p_project_ids IS NULL
       OR pg_catalog.array_position(p_project_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding replay-scope request';
    END IF;

    SELECT pg_catalog.cardinality(p_project_ids)::bigint,
           pg_catalog.count(DISTINCT requested.project_id)::bigint
    INTO v_project_count, v_unique_project_count
    FROM pg_catalog.unnest(p_project_ids) AS requested(project_id);
    IF v_project_count IS DISTINCT FROM v_unique_project_count THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'duplicate Identity offboarding replay project';
    END IF;

    SELECT pg_catalog.count(project.id)::bigint,
           pg_catalog.count(project.id) FILTER (
               WHERE project.owner_id = p_actor_id
           )::bigint
    INTO v_existing_count, v_owned_count
    FROM pg_catalog.unnest(p_project_ids) AS requested(project_id)
    LEFT JOIN public.projects AS project ON project.id = requested.project_id;

    RETURN QUERY SELECT
        v_existing_count = v_project_count,
        v_owned_count = v_project_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.identity_offboarding_replay_scope(
    uuid, uuid[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.transfer_identity_offboarding_owners(
    p_target_id uuid,
    p_changes jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_change jsonb;
    v_project_id uuid;
    v_expected_owner_id uuid;
    v_receiver_id uuid;
    v_updated_id uuid;
    v_changed_ids uuid[] := ARRAY[]::uuid[];
    v_seen_ids uuid[] := ARRAY[]::uuid[];
    v_field_count integer;
    v_rows integer;
BEGIN
    IF p_target_id IS NULL
       OR pg_catalog.jsonb_typeof(p_changes) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding owner-transfer request';
    END IF;

    FOR v_change IN SELECT item.value FROM pg_catalog.jsonb_array_elements(p_changes) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_change) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding owner-transfer item';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(v_change);
        IF v_field_count <> 3
           OR NOT (v_change ?& ARRAY[
               'project_id', 'expected_owner_id', 'receiver_id'
           ])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_change) AS field(name)
               WHERE field.name NOT IN (
                   'project_id', 'expected_owner_id', 'receiver_id'
               )
           )
           OR pg_catalog.jsonb_typeof(v_change->'project_id') IS DISTINCT FROM 'string'
           OR (v_change->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_change->'expected_owner_id') IS DISTINCT FROM 'string'
           OR (v_change->>'expected_owner_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_change->'receiver_id') IS DISTINCT FROM 'string'
           OR (v_change->>'receiver_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding owner-transfer item';
        END IF;

        v_project_id := (v_change->>'project_id')::uuid;
        v_expected_owner_id := (v_change->>'expected_owner_id')::uuid;
        v_receiver_id := (v_change->>'receiver_id')::uuid;
        IF v_expected_owner_id IS DISTINCT FROM p_target_id
           OR v_receiver_id = p_target_id
           OR v_project_id = ANY(v_seen_ids) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding owner-transfer binding';
        END IF;
        v_seen_ids := pg_catalog.array_append(v_seen_ids, v_project_id);

        UPDATE public.projects AS project
        SET owner_id = v_receiver_id,
            updated_at = CURRENT_TIMESTAMP
        WHERE project.id = v_project_id
          AND project.owner_id = v_expected_owner_id
        RETURNING project.id INTO v_updated_id;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        IF v_rows <> 1 OR v_updated_id IS DISTINCT FROM v_project_id THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity offboarding owner-transfer result';
        END IF;
        v_changed_ids := pg_catalog.array_append(v_changed_ids, v_updated_id);
    END LOOP;

    RETURN pg_catalog.jsonb_build_object(
        'changed_project_ids', pg_catalog.to_jsonb(v_changed_ids),
        'changed_count', pg_catalog.cardinality(v_changed_ids)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.transfer_identity_offboarding_owners(
    uuid, jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.lock_identity_offboarding_work(
    p_target_user_id uuid,
    p_project_ids uuid[]
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_batch_id uuid;
    v_task_id uuid;
    v_lock_id uuid;
BEGIN
    IF p_target_user_id IS NULL OR p_project_ids IS NULL
       OR pg_catalog.array_position(p_project_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding Labeling lock request';
    END IF;

    FOR v_batch_id IN
        SELECT batch.id
        FROM public.task_batches AS batch
        WHERE batch.project_id = ANY(p_project_ids)
          AND (batch.annotator_id = p_target_user_id
               OR batch.reviewer_id = p_target_user_id)
        ORDER BY batch.id
        FOR UPDATE OF batch NOWAIT
    LOOP
        NULL;
    END LOOP;

    FOR v_task_id IN
        SELECT task.id
        FROM public.tasks AS task
        WHERE task.assignee_id = p_target_user_id
           OR task.reviewer_id = p_target_user_id
           OR EXISTS (
               SELECT 1 FROM public.task_locks AS task_lock
               WHERE task_lock.task_id = task.id
                 AND task_lock.user_id = p_target_user_id
           )
        ORDER BY task.id
        FOR UPDATE OF task NOWAIT
    LOOP
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'aap:task-edit-lock:' || v_task_id::text, 0
            )
        ) THEN
            RETURN false;
        END IF;
    END LOOP;

    FOR v_lock_id IN
        SELECT task_lock.id
        FROM public.task_locks AS task_lock
        WHERE task_lock.user_id = p_target_user_id
        ORDER BY task_lock.id
        FOR UPDATE OF task_lock NOWAIT
    LOOP
        NULL;
    END LOOP;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_identity_offboarding_work(
    uuid, uuid[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.identity_offboarding_facts(
    p_target_id uuid
)
RETURNS TABLE(projects jsonb, memberships jsonb, candidate_memberships jsonb)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project_ids uuid[];
    v_projects jsonb;
    v_memberships jsonb;
    v_candidate_memberships jsonb;
BEGIN
    IF p_target_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding Workspace facts request';
    END IF;

    WITH derived AS MATERIALIZED (
        SELECT project.id AS project_id
        FROM public.projects AS project
        WHERE project.owner_id = p_target_id
        UNION
        SELECT member.project_id
        FROM public.project_members AS member
        WHERE member.user_id = p_target_id
        UNION
        SELECT refs.project_id
        FROM aap_labeling_v1.identity_directory_project_refs(
            ARRAY[p_target_id]::uuid[]
        ) AS refs
        WHERE refs.user_id = p_target_id
    )
    SELECT COALESCE(
               pg_catalog.array_agg(project.id ORDER BY project.id),
               ARRAY[]::uuid[]
           )
    INTO v_project_ids
    FROM derived
    JOIN public.projects AS project ON project.id = derived.project_id;

    SELECT COALESCE(
               pg_catalog.jsonb_agg(
                   pg_catalog.jsonb_build_object(
                       'id', project.id,
                       'name', project.name,
                       'owner_id', project.owner_id
                   ) ORDER BY project.id
               ),
               '[]'::jsonb
           )
    INTO v_projects
    FROM public.projects AS project
    WHERE project.id = ANY(v_project_ids);

    SELECT COALESCE(
               pg_catalog.jsonb_agg(
                   pg_catalog.jsonb_build_object(
                       'id', member.id,
                       'project_id', member.project_id,
                       'role', member.role
                   ) ORDER BY member.project_id, member.role
               ),
               '[]'::jsonb
           )
    INTO v_memberships
    FROM public.project_members AS member
    WHERE member.user_id = p_target_id;

    SELECT COALESCE(
               pg_catalog.jsonb_agg(
                   pg_catalog.jsonb_build_object(
                       'project_id', member.project_id,
                       'user_id', member.user_id,
                       'role', member.role
                   ) ORDER BY member.project_id, member.user_id
               ),
               '[]'::jsonb
           )
    INTO v_candidate_memberships
    FROM public.project_members AS member
    WHERE member.project_id = ANY(v_project_ids)
      AND member.user_id <> p_target_id
      AND member.role IN ('annotator', 'reviewer');

    RETURN QUERY SELECT v_projects, v_memberships, v_candidate_memberships;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.identity_offboarding_facts(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.identity_offboarding_facts(
    p_target_user_id uuid
)
RETURNS TABLE(batches jsonb, tasks jsonb, locks jsonb)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_batches jsonb;
    v_tasks jsonb;
    v_locks jsonb;
BEGIN
    IF p_target_user_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding Labeling facts request';
    END IF;

    SELECT COALESCE(
               pg_catalog.jsonb_agg(
                   pg_catalog.jsonb_build_object(
                       'id', batch.id,
                       'project_id', batch.project_id,
                       'name', batch.name,
                       'status', batch.status,
                       'annotator_id', batch.annotator_id,
                       'reviewer_id', batch.reviewer_id,
                       'assigned_user_ids', batch.assigned_user_ids
                   ) ORDER BY batch.project_id, batch.id
               ),
               '[]'::jsonb
           )
    INTO v_batches
    FROM public.task_batches AS batch
    WHERE (batch.annotator_id = p_target_user_id
           OR batch.reviewer_id = p_target_user_id)
      AND batch.status <> 'archived';

    SELECT COALESCE(
               pg_catalog.jsonb_agg(
                   pg_catalog.jsonb_build_object(
                       'id', task.id,
                       'project_id', task.project_id,
                       'batch_id', task.batch_id,
                       'status', task.status,
                       'assignee_id', task.assignee_id,
                       'reviewer_id', task.reviewer_id,
                       'version', task.version
                   ) ORDER BY task.project_id ASC NULLS LAST, task.id
               ),
               '[]'::jsonb
           )
    INTO v_tasks
    FROM public.tasks AS task
    WHERE task.assignee_id = p_target_user_id
       OR task.reviewer_id = p_target_user_id
       OR EXISTS (
           SELECT 1 FROM public.task_locks AS task_lock
           WHERE task_lock.task_id = task.id
             AND task_lock.user_id = p_target_user_id
       );

    SELECT COALESCE(
               pg_catalog.jsonb_agg(
                   pg_catalog.jsonb_build_object(
                       'id', task_lock.id,
                       'task_id', task_lock.task_id,
                       'expire_at', task_lock.expire_at
                   ) ORDER BY task_lock.task_id, task_lock.id
               ),
               '[]'::jsonb
           )
    INTO v_locks
    FROM public.task_locks AS task_lock
    WHERE task_lock.user_id = p_target_user_id;

    RETURN QUERY SELECT v_batches, v_tasks, v_locks;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.identity_offboarding_facts(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.transfer_identity_offboarding_work(
    p_target_user_id uuid,
    p_assignments jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_assignment jsonb;
    v_project_id uuid;
    v_role text;
    v_receiver_id uuid;
    v_batch_ids uuid[];
    v_task_ids uuid[];
    v_actual_batch_ids uuid[];
    v_actual_task_ids uuid[];
    v_results jsonb := '[]'::jsonb;
    v_seen_keys text[] := ARRAY[]::text[];
    v_key text;
    v_field_count integer;
    v_item_count integer;
BEGIN
    IF p_target_user_id IS NULL
       OR pg_catalog.jsonb_typeof(p_assignments) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding Labeling transfer request';
    END IF;

    FOR v_assignment IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(p_assignments) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_assignment) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding Labeling transfer item';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(v_assignment);
        IF v_field_count <> 5
           OR NOT (v_assignment ?& ARRAY[
               'project_id', 'role', 'receiver_id', 'batch_ids', 'task_ids'
           ])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_assignment) AS field(name)
               WHERE field.name NOT IN (
                   'project_id', 'role', 'receiver_id', 'batch_ids', 'task_ids'
               )
           )
           OR pg_catalog.jsonb_typeof(v_assignment->'project_id') IS DISTINCT FROM 'string'
           OR (v_assignment->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_assignment->'role') IS DISTINCT FROM 'string'
           OR v_assignment->>'role' NOT IN ('annotator', 'reviewer')
           OR pg_catalog.jsonb_typeof(v_assignment->'receiver_id') IS DISTINCT FROM 'string'
           OR (v_assignment->>'receiver_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_assignment->'batch_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(v_assignment->'task_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding Labeling transfer item';
        END IF;

        v_project_id := (v_assignment->>'project_id')::uuid;
        v_role := v_assignment->>'role';
        v_receiver_id := (v_assignment->>'receiver_id')::uuid;
        v_key := v_project_id::text || ':' || v_role;
        IF v_receiver_id = p_target_user_id OR v_key = ANY(v_seen_keys) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding Labeling transfer binding';
        END IF;
        v_seen_keys := pg_catalog.array_append(v_seen_keys, v_key);

        SELECT COALESCE(pg_catalog.array_agg(parsed.id ORDER BY parsed.id), ARRAY[]::uuid[])
        INTO v_batch_ids
        FROM (
            SELECT (item.value #>> '{}')::uuid AS id
            FROM pg_catalog.jsonb_array_elements(v_assignment->'batch_ids') AS item(value)
            WHERE pg_catalog.jsonb_typeof(item.value) = 'string'
              AND (item.value #>> '{}') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        ) AS parsed;
        SELECT pg_catalog.count(*)::integer INTO v_item_count
        FROM pg_catalog.jsonb_array_elements(v_assignment->'batch_ids') AS item(value);
        IF v_item_count <> pg_catalog.cardinality(v_batch_ids) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding batch ID list';
        END IF;
        SELECT COALESCE(pg_catalog.array_agg(parsed.id ORDER BY parsed.id), ARRAY[]::uuid[])
        INTO v_task_ids
        FROM (
            SELECT (item.value #>> '{}')::uuid AS id
            FROM pg_catalog.jsonb_array_elements(v_assignment->'task_ids') AS item(value)
            WHERE pg_catalog.jsonb_typeof(item.value) = 'string'
              AND (item.value #>> '{}') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        ) AS parsed;
        SELECT pg_catalog.count(*)::integer INTO v_item_count
        FROM pg_catalog.jsonb_array_elements(v_assignment->'task_ids') AS item(value);
        IF v_item_count <> pg_catalog.cardinality(v_task_ids) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding task ID list';
        END IF;

        IF v_role = 'annotator' THEN
            WITH changed AS (
                UPDATE public.task_batches AS batch
                SET annotator_id = v_receiver_id,
                    assigned_user_ids = pg_catalog.to_jsonb(
                        array_remove(
                            ARRAY[v_receiver_id::text, batch.reviewer_id::text],
                            NULL::text
                        )
                    ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE batch.id = ANY(v_batch_ids)
                  AND batch.project_id = v_project_id
                  AND batch.annotator_id = p_target_user_id
                  AND batch.status <> 'archived'
                RETURNING batch.id
            )
            SELECT COALESCE(pg_catalog.array_agg(changed.id ORDER BY changed.id), ARRAY[]::uuid[])
            INTO v_actual_batch_ids
            FROM changed;

            WITH changed AS (
                UPDATE public.tasks AS task
                SET assignee_id = v_receiver_id,
                    assigned_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task.id = ANY(v_task_ids)
                  AND task.project_id = v_project_id
                  AND task.assignee_id = p_target_user_id
                  AND task.status IN ('pending', 'in_progress', 'review', 'rejected')
                RETURNING task.id
            )
            SELECT COALESCE(pg_catalog.array_agg(changed.id ORDER BY changed.id), ARRAY[]::uuid[])
            INTO v_actual_task_ids
            FROM changed;
        ELSE
            WITH changed AS (
                UPDATE public.task_batches AS batch
                SET reviewer_id = v_receiver_id,
                    assigned_user_ids = pg_catalog.to_jsonb(
                        array_remove(
                            ARRAY[batch.annotator_id::text, v_receiver_id::text],
                            NULL::text
                        )
                    ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE batch.id = ANY(v_batch_ids)
                  AND batch.project_id = v_project_id
                  AND batch.reviewer_id = p_target_user_id
                  AND batch.status <> 'archived'
                RETURNING batch.id
            )
            SELECT COALESCE(pg_catalog.array_agg(changed.id ORDER BY changed.id), ARRAY[]::uuid[])
            INTO v_actual_batch_ids
            FROM changed;

            WITH changed AS (
                UPDATE public.tasks AS task
                SET reviewer_id = v_receiver_id,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task.id = ANY(v_task_ids)
                  AND task.project_id = v_project_id
                  AND task.reviewer_id = p_target_user_id
                  AND task.status IN ('pending', 'in_progress', 'review', 'rejected')
                RETURNING task.id
            )
            SELECT COALESCE(pg_catalog.array_agg(changed.id ORDER BY changed.id), ARRAY[]::uuid[])
            INTO v_actual_task_ids
            FROM changed;
        END IF;

        IF v_actual_batch_ids IS DISTINCT FROM v_batch_ids
           OR v_actual_task_ids IS DISTINCT FROM v_task_ids THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Identity offboarding Labeling transfer result mismatch';
        END IF;

        v_results := v_results || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'project_id', v_project_id,
                'role', v_role,
                'batch_ids', pg_catalog.to_jsonb(v_actual_batch_ids),
                'task_ids', pg_catalog.to_jsonb(v_actual_task_ids),
                'task_count', pg_catalog.cardinality(v_actual_task_ids)
            )
        );
    END LOOP;
    RETURN v_results;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.transfer_identity_offboarding_work(
    uuid, jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.release_identity_offboarding_task_locks(
    p_target_user_id uuid
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted bigint;
BEGIN
    IF p_target_user_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding task-lock release request';
    END IF;
    DELETE FROM public.task_locks AS task_lock
    WHERE task_lock.user_id = p_target_user_id;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.release_identity_offboarding_task_locks(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_identity_offboarding_event(
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
    v_audit_id bigint;
    v_revoked_count bigint;
    v_item jsonb;
    v_nested jsonb;
    v_key text;
    v_task_count bigint;
    v_lock_count bigint;
    v_seen_keys text[] := ARRAY[]::text[];
    v_seen_batch_ids text[] := ARRAY[]::text[];
BEGIN
    IF p_target_user_id IS NULL
       OR pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object'
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding Audit request';
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
            MESSAGE = 'invalid Identity offboarding Audit context';
    END IF;
    v_ip := NULLIF(p_context->>'ip', '')::pg_catalog.inet;

    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding Audit actor';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_actor);
    IF v_field_count <> 3
       OR NOT (p_actor ?& ARRAY['actor_id', 'email', 'role'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_actor) AS field(name)
           WHERE field.name NOT IN ('actor_id', 'email', 'role')
       )
       OR pg_catalog.jsonb_typeof(p_actor->'actor_id') IS DISTINCT FROM 'string'
       OR (p_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_actor->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_actor->>'email') > 255
       OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
       OR p_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding Audit actor';
    END IF;
    v_actor_id := (p_actor->>'actor_id')::uuid;
    IF v_actor_id = p_target_user_id THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Identity offboarding Audit actor cannot equal target';
    END IF;
    v_actor_email := p_actor->>'email';
    v_actor_role := p_actor->>'role';

    IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding Audit detail';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_detail);
    IF v_field_count <> 5
       OR NOT (p_detail ?& ARRAY[
           'mode', 'reason_present', 'transfers', 'unresolved',
           'revoked_api_key_count'
       ])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_detail) AS field(name)
           WHERE field.name NOT IN (
               'mode', 'reason_present', 'transfers', 'unresolved',
               'revoked_api_key_count'
           )
       )
       OR pg_catalog.jsonb_typeof(p_detail->'mode') IS DISTINCT FROM 'string'
       OR p_detail->>'mode' NOT IN ('handoff', 'emergency_suspend')
       OR pg_catalog.jsonb_typeof(p_detail->'reason_present') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_detail->'transfers') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(p_detail->'unresolved') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(p_detail->'revoked_api_key_count') IS DISTINCT FROM 'number'
       OR (p_detail->>'revoked_api_key_count') !~ '^(0|[1-9][0-9]*)$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding Audit detail';
    END IF;
    BEGIN
        v_revoked_count := (p_detail->>'revoked_api_key_count')::bigint;
    EXCEPTION WHEN numeric_value_out_of_range THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding Audit key count';
    END;

    IF (p_detail->>'mode' = 'handoff'
        AND pg_catalog.jsonb_array_length(p_detail->'unresolved') <> 0)
       OR (p_detail->>'mode' = 'emergency_suspend'
           AND pg_catalog.jsonb_array_length(p_detail->'transfers') <> 0) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding Audit mode result';
    END IF;

    FOR v_item IN
        SELECT item.value
        FROM pg_catalog.jsonb_array_elements(p_detail->'transfers') AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding Audit transfer';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(v_item);
        IF v_field_count <> 6
           OR NOT (v_item ?& ARRAY[
               'project_id', 'role', 'receiver_id', 'batch_ids',
               'task_count', 'lock_count'
           ])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_item) AS field(name)
               WHERE field.name NOT IN (
                   'project_id', 'role', 'receiver_id', 'batch_ids',
                   'task_count', 'lock_count'
               )
           )
           OR pg_catalog.jsonb_typeof(v_item->'project_id') IS DISTINCT FROM 'string'
           OR (v_item->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_item->'role') IS DISTINCT FROM 'string'
           OR v_item->>'role' NOT IN ('owner', 'annotator', 'reviewer')
           OR pg_catalog.jsonb_typeof(v_item->'receiver_id') IS DISTINCT FROM 'string'
           OR (v_item->>'receiver_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_item->'batch_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(v_item->'task_count') IS DISTINCT FROM 'number'
           OR (v_item->>'task_count') !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.jsonb_typeof(v_item->'lock_count') IS DISTINCT FROM 'number'
           OR (v_item->>'lock_count') !~ '^(0|[1-9][0-9]*)$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding Audit transfer';
        END IF;
        BEGIN
            v_task_count := (v_item->>'task_count')::bigint;
            v_lock_count := (v_item->>'lock_count')::bigint;
        EXCEPTION WHEN numeric_value_out_of_range THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding Audit transfer counts';
        END;
        v_key := (v_item->>'project_id') || ':' || (v_item->>'role');
        IF v_key = ANY(v_seen_keys) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'duplicate Identity offboarding Audit transfer';
        END IF;
        v_seen_keys := pg_catalog.array_append(v_seen_keys, v_key);
        IF v_item->>'role' = 'owner'
           AND (pg_catalog.jsonb_array_length(v_item->'batch_ids') <> 0
                OR v_task_count <> 0 OR v_lock_count <> 0) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding owner transfer details';
        END IF;
        v_seen_batch_ids := ARRAY[]::text[];
        FOR v_nested IN
            SELECT item.value
            FROM pg_catalog.jsonb_array_elements(v_item->'batch_ids') AS item(value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_nested) IS DISTINCT FROM 'string'
               OR (v_nested #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_nested #>> '{}') = ANY(v_seen_batch_ids) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Identity offboarding Audit batch IDs';
            END IF;
            v_seen_batch_ids := pg_catalog.array_append(
                v_seen_batch_ids, v_nested #>> '{}'
            );
        END LOOP;
    END LOOP;
    FOR v_item IN
        SELECT item.value
        FROM pg_catalog.jsonb_array_elements(p_detail->'unresolved') AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding Audit unresolved item';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(v_item);
        IF v_field_count <> 6
           OR NOT (v_item ?& ARRAY[
               'project_id', 'role', 'reason', 'batch_ids', 'task_count', 'lock_count'
           ])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_item) AS field(name)
               WHERE field.name NOT IN (
                   'project_id', 'role', 'reason', 'batch_ids', 'task_count', 'lock_count'
               )
           )
           OR pg_catalog.jsonb_typeof(v_item->'project_id') IS DISTINCT FROM 'string'
           OR (v_item->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_item->'role') IS DISTINCT FROM 'string'
           OR v_item->>'role' NOT IN ('owner', 'annotator', 'reviewer')
           OR pg_catalog.jsonb_typeof(v_item->'reason') IS DISTINCT FROM 'string'
           OR v_item->>'reason' IS DISTINCT FROM 'emergency_suspension_requires_later_handoff'
           OR pg_catalog.jsonb_typeof(v_item->'batch_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(v_item->'task_count') IS DISTINCT FROM 'number'
           OR (v_item->>'task_count') !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.jsonb_typeof(v_item->'lock_count') IS DISTINCT FROM 'number'
           OR (v_item->>'lock_count') !~ '^(0|[1-9][0-9]*)$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding Audit unresolved item';
        END IF;
        BEGIN
            v_task_count := (v_item->>'task_count')::bigint;
            v_lock_count := (v_item->>'lock_count')::bigint;
        EXCEPTION WHEN numeric_value_out_of_range THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding Audit unresolved counts';
        END;
        v_key := (v_item->>'project_id') || ':' || (v_item->>'role');
        IF v_key = ANY(v_seen_keys) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'duplicate Identity offboarding Audit unresolved item';
        END IF;
        v_seen_keys := pg_catalog.array_append(v_seen_keys, v_key);
        IF v_item->>'role' = 'owner'
           AND (pg_catalog.jsonb_array_length(v_item->'batch_ids') <> 0
                OR v_task_count <> 0 OR v_lock_count <> 0) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity offboarding unresolved owner details';
        END IF;
        v_seen_batch_ids := ARRAY[]::text[];
        FOR v_nested IN
            SELECT item.value
            FROM pg_catalog.jsonb_array_elements(v_item->'batch_ids') AS item(value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_nested) IS DISTINCT FROM 'string'
               OR (v_nested #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_nested #>> '{}') = ANY(v_seen_batch_ids) THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid Identity offboarding Audit batch IDs';
            END IF;
            v_seen_batch_ids := pg_catalog.array_append(
                v_seen_batch_ids, v_nested #>> '{}'
            );
        END LOOP;
    END LOOP;

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        v_actor_id, v_actor_email, v_actor_role, 'user.offboard', 'user',
        p_target_user_id::text, p_context->>'method', p_context->>'path', 200,
        v_ip, p_context->>'request_id', p_detail
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_identity_offboarding_event(
    jsonb, uuid, jsonb, jsonb
) FROM PUBLIC;
