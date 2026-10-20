-- Shared Identity scope, immutable suspension receipts and typed Audit.
CREATE SCHEMA aap_identity_suspensions_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_suspensions_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_suspension_operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    operation text NOT NULL CHECK (
        operation IN ('suspend_account', 'reactivate_account')
    ),
    target_user_id uuid NOT NULL,
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
CREATE INDEX ix_identity_suspension_operation_receipts_actor
    ON public.identity_suspension_operation_receipts (actor_id, created_at DESC);
-- aap-statement-break
COMMENT ON TABLE public.identity_suspension_operation_receipts IS
    'Frozen suspension/reactivation results and post-operation lifecycle fences; no User FK or credentials.';
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_suspension_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.lock_identity_suspension_scope(
    p_actor_id uuid,
    p_target_id uuid,
    p_operation text
)
RETURNS TABLE(has_projects boolean, all_projects_owned boolean)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project_ids uuid[] := ARRAY[]::uuid[];
    v_project_id uuid;
    v_has_projects boolean := false;
    v_all_projects_owned boolean := true;
BEGIN
    IF p_actor_id IS NULL OR p_target_id IS NULL OR p_operation IS NULL
       OR p_operation NOT IN ('suspend_account', 'reactivate_account') THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity suspension Workspace scope request';
    END IF;

    WITH refs AS MATERIALIZED (
        SELECT project.id AS project_id
        FROM public.projects AS project
        WHERE project.owner_id = p_target_id
        UNION
        SELECT member.project_id
        FROM public.project_members AS member
        WHERE member.user_id = p_target_id
        UNION
        SELECT labeling_ref.project_id
        FROM aap_labeling_v1.identity_directory_project_refs(
            ARRAY[p_target_id]::uuid[]
        ) AS labeling_ref
        WHERE labeling_ref.user_id = p_target_id
    )
    SELECT COALESCE(
               pg_catalog.array_agg(project.id ORDER BY project.id),
               ARRAY[]::uuid[]
           )
    INTO v_project_ids
    FROM refs
    JOIN public.projects AS project ON project.id = refs.project_id;

    v_has_projects := pg_catalog.cardinality(v_project_ids) > 0;

    IF p_operation = 'suspend_account' THEN
        -- Legacy suspension locks every derived project with KEY SHARE.
        FOR v_project_id IN
            SELECT project.id
            FROM public.projects AS project
            WHERE project.id = ANY(v_project_ids)
            ORDER BY project.id
            FOR KEY SHARE OF project NOWAIT
        LOOP
            NULL;
        END LOOP;
    ELSE
        -- Legacy PA reactivation locks only actor-owned projects with SHARE,
        -- then compares that owned subset with the full derived project set.
        FOR v_project_id IN
            SELECT project.id
            FROM public.projects AS project
            WHERE project.id = ANY(v_project_ids)
              AND project.owner_id = p_actor_id
            ORDER BY project.id
            FOR SHARE OF project NOWAIT
        LOOP
            NULL;
        END LOOP;
    END IF;

    SELECT pg_catalog.count(*) = pg_catalog.cardinality(v_project_ids)
    INTO v_all_projects_owned
    FROM public.projects AS project
    WHERE project.id = ANY(v_project_ids)
      AND project.owner_id = p_actor_id;

    RETURN QUERY SELECT v_has_projects, v_all_projects_owned;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.lock_identity_suspension_scope(
    uuid, uuid, text
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_identity_suspension_event(
    p_event text,
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
    v_count integer;
    v_actor_id uuid;
    v_actor_email text;
    v_actor_role text;
    v_ip inet;
    v_audit_id bigint;
BEGIN
    IF p_event IS NULL OR p_event NOT IN ('user.deactivate', 'user.reactivate')
       OR p_target_user_id IS NULL
       OR pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object'
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity suspension Audit request';
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
            MESSAGE = 'invalid Identity suspension Audit context';
    END IF;
    v_ip := NULLIF(p_context->>'ip', '')::pg_catalog.inet;

    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity suspension Audit actor';
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
            MESSAGE = 'invalid Identity suspension Audit actor';
    END IF;
    v_actor_id := (p_actor->>'actor_id')::uuid;
    v_actor_email := p_actor->>'email';
    v_actor_role := p_actor->>'role';

    IF p_event = 'user.deactivate' THEN
        IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity suspension Audit detail';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_count
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF v_count <> 1 OR NOT (p_detail ? 'email')
           OR pg_catalog.jsonb_typeof(p_detail->'email') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_detail->>'email') NOT BETWEEN 1 AND 255 THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity suspension Audit detail';
        END IF;
    ELSE
        IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity reactivation Audit detail';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_count
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF v_count <> 1 OR NOT (p_detail ? 'reason_present')
           OR pg_catalog.jsonb_typeof(p_detail->'reason_present') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Identity reactivation Audit detail';
        END IF;
    END IF;

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        v_actor_id, v_actor_email, v_actor_role, p_event, 'user',
        p_target_user_id::text, p_context->>'method', p_context->>'path', 200,
        v_ip, p_context->>'request_id', p_detail
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_identity_suspension_event(
    text, jsonb, uuid, jsonb, jsonb
) FROM PUBLIC;
