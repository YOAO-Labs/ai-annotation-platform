-- Narrow cross-owner directory projections and export receipt state.
CREATE SCHEMA aap_identity_directory_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_directory_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_directory_export_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    operation text NOT NULL CHECK (operation = 'export_users'),
    request_fingerprint text NOT NULL CHECK (
        request_fingerprint COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    derivation_id text NOT NULL CHECK (
        derivation_id COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    audit_id bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE INDEX ix_identity_directory_export_receipts_actor
    ON public.identity_directory_export_receipts (actor_id, created_at DESC);
-- aap-statement-break
COMMENT ON TABLE public.identity_directory_export_receipts IS
    'Frozen eight-column directory exports used to suppress duplicate export Audit rows.';
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_directory_export_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.identity_directory_owned_member_ids(
    p_actor_id uuid
)
RETURNS SETOF uuid
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT DISTINCT member.user_id
    FROM public.project_members AS member
    JOIN public.projects AS project ON project.id = member.project_id
    WHERE project.owner_id = p_actor_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.identity_directory_owned_member_ids(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.identity_directory_project_member_ids(
    p_project_id uuid
)
RETURNS SETOF uuid
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT member.user_id
    FROM public.project_members AS member
    WHERE member.project_id = p_project_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.identity_directory_project_member_ids(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.identity_directory_project_refs(
    p_user_ids uuid[]
)
RETURNS TABLE(user_id uuid, project_id uuid)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_user_ids IS NULL OR pg_catalog.cardinality(p_user_ids) > 500
       OR pg_catalog.array_position(p_user_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid directory lifecycle reference request';
    END IF;
    RETURN QUERY
    SELECT DISTINCT ref.user_id, ref.project_id
    FROM (
        SELECT batch.annotator_id AS user_id, batch.project_id
        FROM public.task_batches AS batch
        WHERE batch.annotator_id = ANY(p_user_ids)
        UNION ALL
        SELECT batch.reviewer_id, batch.project_id
        FROM public.task_batches AS batch
        WHERE batch.reviewer_id = ANY(p_user_ids)
        UNION ALL
        SELECT task.assignee_id, task.project_id
        FROM public.tasks AS task
        WHERE task.assignee_id = ANY(p_user_ids)
        UNION ALL
        SELECT task.reviewer_id, task.project_id
        FROM public.tasks AS task
        WHERE task.reviewer_id = ANY(p_user_ids)
        UNION ALL
        SELECT task_lock.user_id, task.project_id
        FROM public.task_locks AS task_lock
        JOIN public.tasks AS task ON task.id = task_lock.task_id
        WHERE task_lock.user_id = ANY(p_user_ids)
    ) AS ref
    WHERE ref.user_id IS NOT NULL AND ref.project_id IS NOT NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.identity_directory_project_refs(uuid[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.identity_directory_lifecycle_blocked(
    p_actor_id uuid,
    p_user_ids uuid[]
)
RETURNS SETOF uuid
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_actor_id IS NULL OR p_user_ids IS NULL
       OR pg_catalog.cardinality(p_user_ids) > 500
       OR pg_catalog.array_position(p_user_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid directory lifecycle scope request';
    END IF;
    RETURN QUERY
    WITH refs AS (
        SELECT project.owner_id AS user_id, project.id AS project_id
        FROM public.projects AS project
        WHERE project.owner_id = ANY(p_user_ids)
        UNION
        SELECT member.user_id, member.project_id
        FROM public.project_members AS member
        WHERE member.user_id = ANY(p_user_ids)
        UNION
        SELECT labeling_ref.user_id, labeling_ref.project_id
        FROM aap_labeling_v1.identity_directory_project_refs(p_user_ids)
            AS labeling_ref
    )
    SELECT DISTINCT ref.user_id
    FROM refs AS ref
    JOIN public.projects AS project ON project.id = ref.project_id
    WHERE ref.user_id = ANY(p_user_ids)
      AND project.owner_id <> p_actor_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.identity_directory_lifecycle_blocked(
    uuid, uuid[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_identity_directory_export(
    p_actor jsonb,
    p_context jsonb,
    p_detail jsonb
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
    v_detail_count bigint;
    v_ip inet;
    v_audit_id bigint;
BEGIN
    IF pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object'
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity directory Audit context';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_context);
    IF v_count <> 4 OR NOT (p_context ?& ARRAY['method', 'path', 'ip', 'request_id'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_context) AS field(name)
           WHERE field.name NOT IN ('method', 'path', 'ip', 'request_id')
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity directory Audit context';
    END IF;
    v_ip := NULLIF(p_context->>'ip', '')::pg_catalog.inet;

    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity directory Audit actor';
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
       OR pg_catalog.char_length(p_actor->>'email') NOT BETWEEN 1 AND 255
       OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
       OR p_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity directory Audit actor';
    END IF;
    v_actor_id := (p_actor->>'actor_id')::uuid;
    v_actor_email := p_actor->>'email';
    v_actor_role := p_actor->>'role';

    IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity directory Audit detail';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_detail);
    IF v_count <> 5 OR NOT (p_detail ?& ARRAY[
        'format', 'count', 'actor_email', 'ip', 'request_id'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_detail) AS field(name)
        WHERE field.name NOT IN ('format', 'count', 'actor_email', 'ip', 'request_id')
    ) OR p_detail->>'format' NOT IN ('csv', 'json')
       OR pg_catalog.jsonb_typeof(p_detail->'count') IS DISTINCT FROM 'number'
       OR (p_detail->>'count') !~ '^(0|[1-9][0-9]*)$'
       OR (p_detail->>'count')::numeric > 9223372036854775807
       OR p_detail->>'actor_email' IS DISTINCT FROM v_actor_email
       OR p_detail->>'ip' IS DISTINCT FROM p_context->>'ip'
       OR p_detail->>'request_id' IS DISTINCT FROM p_context->>'request_id' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity directory Audit detail';
    END IF;
    v_detail_count := (p_detail->>'count')::bigint;

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        v_actor_id, v_actor_email, v_actor_role, 'user.export', 'user', NULL,
        p_context->>'method', p_context->>'path', 200,
        v_ip, p_context->>'request_id', p_detail
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_identity_directory_export(
    jsonb, jsonb, jsonb
) FROM PUBLIC;
