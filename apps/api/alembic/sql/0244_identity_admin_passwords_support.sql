-- Narrow Workspace/Audit capabilities and durable reset receipts.
CREATE SCHEMA aap_identity_admin_passwords_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_admin_passwords_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_admin_password_reset_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    target_user_id uuid NOT NULL,
    operation text NOT NULL CHECK (operation = 'admin_reset_password'),
    request_fingerprint text NOT NULL CHECK (
        request_fingerprint COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    derivation_id text NOT NULL CHECK (
        derivation_id COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    target_email text NOT NULL CHECK (pg_catalog.char_length(target_email) <= 255),
    password_admin_reset_at timestamptz NOT NULL,
    password_state_fingerprint text NOT NULL CHECK (
        password_state_fingerprint COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    audit_id bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE INDEX ix_identity_admin_password_reset_receipts_actor
    ON public.identity_admin_password_reset_receipts (actor_id, created_at DESC);
-- aap-statement-break
COMMENT ON TABLE public.identity_admin_password_reset_receipts IS
    'Idempotency receipts for administrator password resets; no credentials, plaintext password or User FK.';
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_admin_password_reset_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.admin_password_target_in_owned_project(
    p_actor_id uuid,
    p_target_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project_id uuid;
    v_locked_projects uuid[] := ARRAY[]::uuid[];
BEGIN
    IF p_actor_id IS NULL OR p_target_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid administrator password scope request';
    END IF;

    -- Caller already holds the actor and target User rows in UUID order.
    -- Capture only projects in which this target is currently a member, then
    -- fence owner changes before taking the matching ProjectMember locks.
    FOR v_project_id IN
        SELECT project.id
        FROM public.projects AS project
        WHERE project.owner_id = p_actor_id
          AND EXISTS (
              SELECT 1 FROM public.project_members AS member
              WHERE member.project_id = project.id
                AND member.user_id = p_target_id
          )
        ORDER BY project.id
        FOR SHARE OF project NOWAIT
    LOOP
        v_locked_projects := pg_catalog.array_append(v_locked_projects, v_project_id);
    END LOOP;

    PERFORM member.project_id
    FROM public.project_members AS member
    WHERE member.project_id = ANY(v_locked_projects)
      AND member.user_id = p_target_id
    ORDER BY member.project_id, member.user_id
    FOR SHARE OF member NOWAIT;

    -- Only the captured project set may authorize. A new membership/project
    -- visible to a later statement is omitted until a retry; owner transfer
    -- remains blocked by the Project SHARE locks through the caller commit.
    RETURN EXISTS (
        SELECT 1
        FROM public.project_members AS member
        JOIN public.projects AS project ON project.id = member.project_id
        WHERE member.project_id = ANY(v_locked_projects)
          AND member.user_id = p_target_id
          AND project.owner_id = p_actor_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.admin_password_target_in_owned_project(
    uuid, uuid
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_identity_admin_password_reset(
    p_actor jsonb,
    p_target_user_id uuid,
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
    v_target_email text;
    v_target_role text;
    v_ip inet;
    v_audit_id bigint;
BEGIN
    IF p_target_user_id IS NULL
       OR pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object'
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity administrator password Audit context';
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
            MESSAGE = 'invalid Identity administrator password Audit context';
    END IF;
    v_ip := NULLIF(p_context->>'ip', '')::pg_catalog.inet;

    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity administrator password Audit actor';
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
       OR p_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity administrator password Audit actor';
    END IF;
    v_actor_id := (p_actor->>'actor_id')::uuid;
    v_actor_email := p_actor->>'email';
    v_actor_role := p_actor->>'role';

    IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity administrator password Audit detail';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_detail);
    IF v_count <> 2 OR NOT (p_detail ?& ARRAY['target_email', 'target_role'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_detail) AS field(name)
           WHERE field.name NOT IN ('target_email', 'target_role')
       ) OR pg_catalog.jsonb_typeof(p_detail->'target_email') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_detail->>'target_email') > 255
       OR pg_catalog.jsonb_typeof(p_detail->'target_role') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_detail->>'target_role') > 50 THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity administrator password Audit detail';
    END IF;
    v_target_email := p_detail->>'target_email';
    v_target_role := p_detail->>'target_role';

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        v_actor_id, v_actor_email, v_actor_role,
        'user.password_admin_reset', 'user', p_target_user_id::text,
        p_context->>'method', p_context->>'path', 200,
        v_ip, p_context->>'request_id',
        pg_catalog.jsonb_build_object(
            'target_email', v_target_email,
            'target_role', v_target_role
        )
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_identity_admin_password_reset(
    jsonb, uuid, jsonb, jsonb
) FROM PUBLIC;
