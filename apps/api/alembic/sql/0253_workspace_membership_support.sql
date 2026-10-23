-- Narrow Identity and Audit capabilities for Workspace membership ownership.
CREATE SCHEMA aap_workspace_membership_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_workspace_membership_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.workspace_membership_operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    project_id uuid NOT NULL,
    operation text NOT NULL CHECK (
        operation IN (
            'add_member', 'change_role', 'remove_member'
        )
    ),
    request_hash text NOT NULL CHECK (
        request_hash COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    result jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(result) = 'object'
    ),
    audit_id bigint NOT NULL CHECK (audit_id > 0),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
COMMENT ON TABLE public.workspace_membership_operation_receipts IS
    'Immutable private successful Workspace membership replies; no User, Project or member foreign keys.';
-- aap-statement-break
REVOKE ALL ON TABLE public.workspace_membership_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.read_workspace_membership_accounts(
    p_account_ids uuid[]
)
RETURNS TABLE(
    id uuid,
    name text,
    email text,
    role text,
    is_active boolean
)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_account_ids IS NULL
       OR (
           pg_catalog.cardinality(p_account_ids) > 0
           AND pg_catalog.array_ndims(p_account_ids) IS DISTINCT FROM 1
       )
       OR pg_catalog.array_position(p_account_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace membership account projection';
    END IF;

    RETURN QUERY
    SELECT account.id, account.name::text, account.email::text, account.role::text,
           account.is_active
    FROM public.users AS account
    WHERE account.id = ANY(p_account_ids)
    ORDER BY account.id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.read_workspace_membership_accounts(uuid[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_workspace_membership_event(
    p_event text,
    p_actor jsonb,
    p_target_id uuid,
    p_context jsonb,
    p_detail jsonb
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count integer;
    v_actor_id uuid;
    v_actor_email text;
    v_actor_role text;
    v_ip pg_catalog.inet;
    v_audit_id bigint;
BEGIN
    IF p_event IS NULL OR p_event NOT IN (
        'project.member_add',
        'project.member_role_change',
        'project.member_remove'
    ) OR p_target_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace membership Audit event';
    END IF;

    IF pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object'
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace membership Audit context';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_context);
    IF v_count <> 4
       OR NOT (p_context ?& ARRAY['method', 'path', 'ip', 'request_id'])
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.jsonb_object_keys(p_context) AS field(name)
           WHERE field.name NOT IN ('method', 'path', 'ip', 'request_id')
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace membership Audit context';
    END IF;
    v_ip := NULLIF(p_context->>'ip', '')::pg_catalog.inet;

    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace membership Audit actor';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_actor);
    IF v_count <> 3
       OR NOT (p_actor ?& ARRAY['actor_id', 'email', 'role'])
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.jsonb_object_keys(p_actor) AS field(name)
           WHERE field.name NOT IN ('actor_id', 'email', 'role')
       )
       OR pg_catalog.jsonb_typeof(p_actor->'actor_id') IS DISTINCT FROM 'string'
       OR (p_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_actor->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
       OR p_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace membership Audit actor';
    END IF;
    v_actor_id := (p_actor->>'actor_id')::uuid;
    v_actor_email := p_actor->>'email';
    v_actor_role := p_actor->>'role';

    IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace membership Audit detail';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_detail);
    IF p_event = 'project.member_add' THEN
        IF v_count <> 4
           OR NOT (p_detail ?& ARRAY[
               'project_id', 'user_id', 'project_role', 'platform_role'
           ])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(p_detail) AS field(name)
               WHERE field.name NOT IN (
                   'project_id', 'user_id', 'project_role', 'platform_role'
               )
           )
           OR pg_catalog.jsonb_typeof(p_detail->'project_id') IS DISTINCT FROM 'string'
           OR (p_detail->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_detail->'user_id') IS DISTINCT FROM 'string'
           OR (p_detail->>'user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_detail->'project_role') IS DISTINCT FROM 'string'
           OR p_detail->>'project_role' NOT IN ('annotator', 'reviewer', 'viewer')
           OR pg_catalog.jsonb_typeof(p_detail->'platform_role') IS DISTINCT FROM 'string'
           OR p_detail->>'platform_role' NOT IN ('employee', 'viewer') THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace membership add Audit detail';
        END IF;
    ELSIF p_event = 'project.member_role_change' THEN
        IF v_count <> 9
           OR NOT (p_detail ?& ARRAY[
               'project_id', 'user_id', 'from_role', 'to_role', 'version',
               'reason', 'replacement_annotator_id', 'replacement_reviewer_id',
               'released_lock_count'
           ])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(p_detail) AS field(name)
               WHERE field.name NOT IN (
                   'project_id', 'user_id', 'from_role', 'to_role', 'version',
                   'reason', 'replacement_annotator_id', 'replacement_reviewer_id',
                   'released_lock_count'
               )
           )
           OR pg_catalog.jsonb_typeof(p_detail->'project_id') IS DISTINCT FROM 'string'
           OR (p_detail->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_detail->'user_id') IS DISTINCT FROM 'string'
           OR (p_detail->>'user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_detail->'from_role') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_detail->'to_role') IS DISTINCT FROM 'string'
           OR p_detail->>'to_role' NOT IN ('annotator', 'reviewer', 'viewer')
           OR pg_catalog.jsonb_typeof(p_detail->'version') IS DISTINCT FROM 'number'
           OR (p_detail->>'version') !~ '^-?(0|[1-9][0-9]*)$'
           OR pg_catalog.jsonb_typeof(p_detail->'reason') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_detail->'replacement_annotator_id') NOT IN ('null', 'string')
           OR (pg_catalog.jsonb_typeof(p_detail->'replacement_annotator_id') = 'string'
               AND (p_detail->>'replacement_annotator_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(p_detail->'replacement_reviewer_id') NOT IN ('null', 'string')
           OR (pg_catalog.jsonb_typeof(p_detail->'replacement_reviewer_id') = 'string'
               AND (p_detail->>'replacement_reviewer_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(p_detail->'released_lock_count') IS DISTINCT FROM 'number'
           OR (p_detail->>'released_lock_count') !~ '^(0|[1-9][0-9]*)$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace membership role-change Audit detail';
        END IF;
    ELSE
        IF v_count <> 3
           OR NOT (p_detail ?& ARRAY['project_id', 'user_id', 'project_role'])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(p_detail) AS field(name)
               WHERE field.name NOT IN ('project_id', 'user_id', 'project_role')
           )
           OR pg_catalog.jsonb_typeof(p_detail->'project_id') IS DISTINCT FROM 'string'
           OR (p_detail->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_detail->'user_id') IS DISTINCT FROM 'string'
           OR (p_detail->>'user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_detail->'project_role') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace membership remove Audit detail';
        END IF;
    END IF;

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        v_actor_id, v_actor_email, v_actor_role, p_event,
        'project_member', p_target_id::text,
        p_context->>'method', p_context->>'path', NULL, v_ip,
        p_context->>'request_id', p_detail
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_workspace_membership_event(
    text, jsonb, uuid, jsonb, jsonb
) FROM PUBLIC;
