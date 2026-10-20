-- Private owner capabilities and receipt state for the complete invitation
-- lifecycle. The caller retains the shared PostgreSQL transaction.
CREATE SCHEMA aap_identity_invitations_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_invitations_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_invitation_operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid,
    operation text NOT NULL CHECK (operation IN (
        'create_invitation', 'preview_invitation_batch', 'apply_invitation_batch',
        'list_invitations', 'query_invitations', 'invitation_stats',
        'export_invitations', 'revoke_invitation', 'resend_invitation',
        'send_invitation_email', 'resolve_invitation', 'register_invitation',
        'accept_invitation'
    )),
    request_fingerprint text NOT NULL CHECK (
        request_fingerprint COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    derivation_id text NOT NULL CHECK (derivation_id COLLATE "C" ~ '^[0-9a-f]{64}$'),
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    audit_id bigint,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE INDEX ix_identity_invitation_operation_receipts_actor
    ON public.identity_invitation_operation_receipts (actor_id, created_at DESC)
    WHERE actor_id IS NOT NULL;
-- aap-statement-break
COMMENT ON TABLE public.identity_invitation_operation_receipts IS
    'Identity invitation idempotency results; bearer, proof, link and password material are excluded.';
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_invitation_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.invitation_projects(p_project_ids uuid[])
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_projects jsonb;
BEGIN
    IF p_project_ids IS NULL OR pg_catalog.cardinality(p_project_ids) > 500
       OR pg_catalog.array_position(p_project_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation project request';
    END IF;
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'id', project.id, 'name', project.name, 'owner_id', project.owner_id
            ) ORDER BY project.id
        ), '[]'::jsonb
    ) INTO v_projects
    FROM public.projects AS project
    WHERE project.id = ANY(p_project_ids);
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-invitation-projects/v1',
        'ok', true, 'data', pg_catalog.jsonb_build_object('projects', v_projects)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.invitation_projects(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.lock_invitation_project(
    p_project_id uuid, p_expected_owner_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project record;
BEGIN
    IF p_project_id IS NULL OR p_expected_owner_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation project lock';
    END IF;
    BEGIN
        SELECT project.id, project.name, project.owner_id
        INTO v_project
        FROM public.projects AS project
        WHERE project.id = p_project_id
        FOR NO KEY UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RAISE;
    END;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-invitation-project/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'project_missing')
        );
    END IF;
    IF v_project.owner_id IS DISTINCT FROM p_expected_owner_id THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-invitation-project/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'project_scope_changed')
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-invitation-project/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'id', v_project.id, 'name', v_project.name, 'owner_id', v_project.owner_id
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.lock_invitation_project(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.read_invitation_membership(
    p_project_id uuid, p_user_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.project_members AS member
        WHERE member.project_id = p_project_id AND member.user_id = p_user_id
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.read_invitation_membership(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.grant_invitation_membership(
    p_actor_id uuid,
    p_project_id uuid,
    p_project_role text,
    p_user_id uuid,
    p_expected_owner_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project jsonb;
    v_actor record;
    v_user record;
    v_owner record;
    v_inserted integer;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL OR p_user_id IS NULL
       OR p_expected_owner_id IS NULL
       OR p_project_role NOT IN ('annotator', 'reviewer', 'viewer') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation membership';
    END IF;

    -- Identity holds the sorted actor/owner/target User row locks before this
    -- capability is called. Read only the closed Identity projection here.
    SELECT facts.actor_id, facts.platform_role, facts.is_active
    INTO v_actor
    FROM aap_identity_v1.read_actor_facts(p_actor_id) AS facts;
    SELECT facts.actor_id, facts.platform_role, facts.is_active
    INTO v_user
    FROM aap_identity_v1.read_actor_facts(p_user_id) AS facts;
    SELECT facts.actor_id, facts.platform_role, facts.is_active
    INTO v_owner
    FROM aap_identity_v1.read_actor_facts(p_expected_owner_id) AS facts;
    IF NOT FOUND OR v_actor.actor_id IS NULL OR v_user.actor_id IS NULL
       OR v_owner.actor_id IS NULL OR NOT v_actor.is_active OR NOT v_user.is_active
       OR NOT v_owner.is_active OR v_actor.platform_role NOT IN ('super_admin','project_admin')
       OR v_owner.platform_role NOT IN ('super_admin','project_admin')
       OR v_user.platform_role NOT IN ('employee','viewer')
       OR (v_user.platform_role = 'viewer' AND p_project_role <> 'viewer')
       OR (v_actor.platform_role = 'project_admin' AND p_actor_id <> p_expected_owner_id) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-invitation-membership/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'current_scope_invalid')
        );
    END IF;

    v_project := aap_workspace_v1.lock_invitation_project(
        p_project_id, p_expected_owner_id
    );
    IF v_project->>'schema' IS DISTINCT FROM 'aap-workspace-invitation-project/v1'
       OR v_project->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-invitation-membership/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', COALESCE(v_project #>> '{error,code}', 'project_missing')
            )
        );
    END IF;
    IF aap_workspace_v1.read_invitation_membership(p_project_id, p_user_id) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-invitation-membership/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'membership_exists')
        );
    END IF;

    INSERT INTO public.project_members (
        id, project_id, user_id, role, assigned_by
    ) VALUES (
        pg_catalog.gen_random_uuid(), p_project_id, p_user_id, p_project_role, p_actor_id
    ) ON CONFLICT (project_id, user_id) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    IF v_inserted <> 1 THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-invitation-membership/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'membership_exists')
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-invitation-membership/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'project_id', p_project_id, 'user_id', p_user_id, 'role', p_project_role
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.grant_invitation_membership(
    uuid, uuid, text, uuid, uuid
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.invitation_work_summary(
    p_project_id uuid, p_user_id uuid, p_project_member_role text
)
RETURNS bigint
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count bigint := 0;
BEGIN
    IF p_project_id IS NULL OR p_user_id IS NULL
       OR p_project_member_role NOT IN ('annotator', 'reviewer', 'viewer') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation work summary';
    END IF;
    IF p_project_member_role = 'annotator' THEN
        SELECT pg_catalog.count(*) INTO v_count
        FROM public.task_batches AS batch
        WHERE batch.project_id = p_project_id
          AND batch.status IN ('active', 'annotating', 'rejected')
          AND batch.annotator_id = p_user_id;
    ELSIF p_project_member_role = 'reviewer' THEN
        SELECT pg_catalog.count(*) INTO v_count
        FROM public.task_batches AS batch
        WHERE batch.project_id = p_project_id
          AND batch.status IN ('active', 'annotating', 'reviewing')
          AND batch.reviewer_id = p_user_id;
    END IF;
    RETURN v_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.invitation_work_summary(uuid, uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_settings_v1.invitation_policy(p_fallbacks jsonb)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_fallback_quota integer;
    v_fallback_ttl integer;
    v_fallback_url text;
    v_fallback_expiry integer;
    v_values jsonb;
    v_quota jsonb;
    v_ttl jsonb;
    v_url jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_fallbacks) IS DISTINCT FROM 'object'
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_fallbacks)) <> 4
       OR NOT (p_fallbacks ?& ARRAY[
           'max_invitations_per_day', 'invitation_ttl_days',
           'frontend_base_url', 'expire_minutes'
       ])
       OR pg_catalog.jsonb_typeof(p_fallbacks->'max_invitations_per_day') IS DISTINCT FROM 'number'
       OR (p_fallbacks->>'max_invitations_per_day') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(p_fallbacks->'invitation_ttl_days') IS DISTINCT FROM 'number'
       OR (p_fallbacks->>'invitation_ttl_days') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(p_fallbacks->'frontend_base_url') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_fallbacks->>'frontend_base_url') < 1
       OR pg_catalog.jsonb_typeof(p_fallbacks->'expire_minutes') IS DISTINCT FROM 'number'
       OR (p_fallbacks->>'expire_minutes') !~ '^(0|[1-9][0-9]*)$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation fallbacks';
    END IF;
    IF (p_fallbacks->>'max_invitations_per_day')::numeric NOT BETWEEN 1 AND 1000
       OR (p_fallbacks->>'invitation_ttl_days')::numeric NOT BETWEEN 1 AND 90
       OR (p_fallbacks->>'expire_minutes')::numeric NOT BETWEEN 1 AND 525600 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation fallback bounds';
    END IF;
    v_fallback_quota := (p_fallbacks->>'max_invitations_per_day')::integer;
    v_fallback_ttl := (p_fallbacks->>'invitation_ttl_days')::integer;
    v_fallback_url := p_fallbacks->>'frontend_base_url';
    v_fallback_expiry := (p_fallbacks->>'expire_minutes')::integer;

    SELECT COALESCE(
        pg_catalog.jsonb_object_agg(setting.key, setting.value_json), '{}'::jsonb
    ) INTO v_values
    FROM public.system_settings AS setting
    WHERE setting.key IN (
        'max_invitations_per_day', 'invitation_ttl_days', 'frontend_base_url'
    ) AND setting.value_json IS NOT NULL
      AND setting.value_json <> 'null'::jsonb;
    v_quota := COALESCE(v_values->'max_invitations_per_day',
                        pg_catalog.to_jsonb(v_fallback_quota));
    v_ttl := COALESCE(v_values->'invitation_ttl_days', pg_catalog.to_jsonb(v_fallback_ttl));
    v_url := COALESCE(v_values->'frontend_base_url', pg_catalog.to_jsonb(v_fallback_url));
    IF pg_catalog.jsonb_typeof(v_quota) IS DISTINCT FROM 'number'
       OR (v_quota #>> '{}') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(v_ttl) IS DISTINCT FROM 'number'
       OR (v_ttl #>> '{}') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(v_url) IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid stored invitation policy';
    END IF;
    IF pg_catalog.char_length(v_url #>> '{}') = 0 THEN
        v_url := pg_catalog.to_jsonb(v_fallback_url);
    END IF;
    IF (v_quota #>> '{}')::numeric NOT BETWEEN 1 AND 1000
       OR (v_ttl #>> '{}')::numeric NOT BETWEEN 1 AND 90 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid stored invitation bounds';
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'max_invitations_per_day', (v_quota #>> '{}')::integer,
        'invitation_ttl_days', (v_ttl #>> '{}')::integer,
        'frontend_base_url', v_url #>> '{}',
        'expire_minutes', v_fallback_expiry
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_settings_v1.invitation_policy(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_identity_invitation_event(
    p_event text, p_actor jsonb, p_target jsonb, p_context jsonb, p_detail jsonb
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
    v_target_type text;
    v_target_id text;
    v_action text;
    v_status smallint;
    v_audit_id bigint;
    v_project_id jsonb;
BEGIN
    IF p_event IS NULL OR p_event NOT IN (
        'user.invite', 'user.bulk_invite', 'user.invite_revoke',
        'user.invite_resend', 'user.register', 'user.invite_accept_existing',
        'invitation.export', 'invitation.send_email'
    ) OR pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object'
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity invitation Audit event';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_context);
    IF v_count <> 4 OR NOT (p_context ?& ARRAY['method','path','ip','request_id'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_context) AS field(name)
           WHERE field.name NOT IN ('method','path','ip','request_id')
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity invitation Audit context';
    END IF;
    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity invitation Audit actor';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_actor);
    IF v_count <> 3 OR NOT (p_actor ?& ARRAY['actor_id','email','role'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_actor) AS field(name)
           WHERE field.name NOT IN ('actor_id','email','role')
       ) OR pg_catalog.jsonb_typeof(p_actor->'actor_id') IS DISTINCT FROM 'string'
       OR (p_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_actor->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_actor->>'email') NOT BETWEEN 1 AND 255
       OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
       OR p_actor->>'role' NOT IN ('super_admin','project_admin','employee','viewer') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity invitation Audit actor';
    END IF;
    v_actor_id := (p_actor->>'actor_id')::uuid;
    v_actor_email := p_actor->>'email';
    v_actor_role := p_actor->>'role';
    IF pg_catalog.jsonb_typeof(p_target) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity invitation Audit target';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_target);
    IF v_count <> 2 OR NOT (p_target ?& ARRAY['type','id'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_target) AS field(name)
           WHERE field.name NOT IN ('type','id')
       ) OR pg_catalog.jsonb_typeof(p_target->'type') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_target->'id') NOT IN ('string','null') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity invitation Audit target';
    END IF;
    v_target_type := p_target->>'type';
    v_target_id := p_target->>'id';

    CASE p_event
        WHEN 'user.invite' THEN
            v_action := p_event; v_status := 201;
            IF v_target_type <> 'user' OR v_target_id IS NULL
               OR pg_catalog.char_length(v_target_id) NOT BETWEEN 3 AND 255
               OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_detail)) <> 3
               OR NOT (p_detail ?& ARRAY['role','group_name','project_id'])
               OR pg_catalog.jsonb_typeof(p_detail->'role') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(p_detail->'group_name') NOT IN ('string','null')
               OR pg_catalog.jsonb_typeof(p_detail->'project_id') NOT IN ('string','null') THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation create Audit detail';
            END IF;
        WHEN 'user.bulk_invite' THEN
            v_action := p_event; v_status := 200;
            IF v_target_type <> 'invitation' OR v_target_id IS NULL
               OR v_target_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_detail)) <> 4
               OR NOT (p_detail ?& ARRAY['email','role','project_id','preview'])
               OR pg_catalog.jsonb_typeof(p_detail->'email') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(p_detail->'role') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(p_detail->'project_id') NOT IN ('string','null')
               OR pg_catalog.jsonb_typeof(p_detail->'preview') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation batch Audit detail';
            END IF;
        WHEN 'user.register' THEN
            v_action := p_event; v_status := 201;
            IF v_target_type <> 'user' OR v_target_id IS NULL
               OR v_target_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR v_target_id <> p_actor->>'actor_id'
               OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_detail)) <> 4
               OR NOT (p_detail ?& ARRAY['email','role','invitation_id','project_id'])
               OR p_detail->>'email' IS DISTINCT FROM v_actor_email
               OR p_detail->>'role' IS DISTINCT FROM v_actor_role
               OR pg_catalog.jsonb_typeof(p_detail->'invitation_id') IS DISTINCT FROM 'string'
               OR (p_detail->>'invitation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(p_detail->'project_id') NOT IN ('string','null') THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation register Audit detail';
            END IF;
        WHEN 'user.invite_accept_existing' THEN
            v_action := p_event; v_status := 200;
            IF v_target_type <> 'invitation' OR v_target_id IS NULL
               OR v_target_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_detail)) <> 2
               OR NOT (p_detail ?& ARRAY['email','project_id'])
               OR p_detail->>'email' IS DISTINCT FROM v_actor_email
               OR pg_catalog.jsonb_typeof(p_detail->'project_id') NOT IN ('string','null') THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation accept Audit detail';
            END IF;
        WHEN 'user.invite_revoke' THEN
            v_action := p_event; v_status := 204;
            IF v_target_type <> 'invitation' OR v_target_id IS NULL
               OR v_target_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_detail)) <> 2
               OR NOT (p_detail ?& ARRAY['email','role'])
               OR pg_catalog.jsonb_typeof(p_detail->'email') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(p_detail->'role') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation revoke Audit detail';
            END IF;
        WHEN 'user.invite_resend' THEN
            v_action := p_event; v_status := 200;
            IF v_target_type <> 'invitation' OR v_target_id IS NULL
               OR v_target_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_detail)) <> 2
               OR NOT (p_detail ?& ARRAY['email','role'])
               OR pg_catalog.jsonb_typeof(p_detail->'email') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(p_detail->'role') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation resend Audit detail';
            END IF;
        WHEN 'invitation.send_email' THEN
            v_action := p_event; v_status := 200;
            IF v_target_type <> 'invitation' OR v_target_id IS NULL
               OR v_target_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_detail)) <> 2
               OR NOT (p_detail ?& ARRAY['email','project_id'])
               OR pg_catalog.jsonb_typeof(p_detail->'email') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(p_detail->'project_id') NOT IN ('string','null') THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation mail Audit detail';
            END IF;
        ELSE
            v_action := p_event; v_status := 200;
            IF v_target_type <> 'invitation' OR v_target_id IS NOT NULL
               OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_detail)) <> 7
               OR NOT (p_detail ?& ARRAY[
                   'format','count','scope','project_id','actor_email','ip','request_id'
               ]) OR p_detail->>'format' NOT IN ('csv','json')
               OR pg_catalog.jsonb_typeof(p_detail->'count') IS DISTINCT FROM 'number'
               OR (p_detail->>'count') !~ '^(0|[1-9][0-9]*)$'
               OR p_detail->>'scope' NOT IN ('me','all')
               OR pg_catalog.jsonb_typeof(p_detail->'project_id') NOT IN ('string','null')
               OR p_detail->>'actor_email' IS DISTINCT FROM v_actor_email
               OR p_detail->>'ip' IS DISTINCT FROM p_context->>'ip'
               OR p_detail->>'request_id' IS DISTINCT FROM p_context->>'request_id' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation export Audit detail';
            END IF;
        END CASE;
    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        v_actor_id, v_actor_email, v_actor_role, v_action, v_target_type,
        v_target_id, p_context->>'method', p_context->>'path', v_status,
        NULLIF(p_context->>'ip', '')::inet, p_context->>'request_id', p_detail
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_identity_invitation_event(
    text, jsonb, jsonb, jsonb, jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1.lock_invitation_mail_scope(
    p_invitation_id uuid, p_expected_token_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_initial record;
    v_invitation record;
    v_workspace jsonb;
    v_project jsonb;
    v_project_ids uuid[];
    v_owner_id uuid;
    v_user_id uuid;
    v_user record;
    v_inviter_role text;
    v_inviter_active boolean;
    v_owner_role text;
    v_owner_active boolean;
    v_member_role text;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_invitation_id IS NULL OR p_expected_token_hash IS NULL
       OR p_expected_token_hash COLLATE "C" !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation mail scope request';
    END IF;
    SELECT invitation.id, invitation.email, invitation.role, invitation.project_role,
           invitation.project_id, invitation.invited_by
    INTO v_initial
    FROM public.user_invitations AS invitation
    WHERE invitation.id = p_invitation_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-invitation-mail-scope/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invitation_superseded')
        );
    END IF;

    IF v_initial.project_id IS NOT NULL THEN
        v_workspace := aap_workspace_v1.invitation_projects(ARRAY[v_initial.project_id]);
        IF v_workspace->>'schema' IS DISTINCT FROM 'aap-workspace-invitation-projects/v1'
           OR v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb
           OR jsonb_array_length(v_workspace #> '{data,projects}') <> 1 THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-invitation-mail-scope/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invitation_not_sendable')
            );
        END IF;
        v_owner_id := (v_workspace #>> '{data,projects,0,owner_id}')::uuid;
    END IF;

    FOR v_user_id IN
        SELECT DISTINCT candidate.id
        FROM unnest(ARRAY[v_initial.invited_by, v_owner_id]::uuid[]) AS candidate(id)
        WHERE candidate.id IS NOT NULL
        ORDER BY candidate.id
    LOOP
        BEGIN
            SELECT account.id, account.role, account.is_active
            INTO v_user
            FROM public.users AS account
            WHERE account.id = v_user_id
            FOR KEY SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE;
        END;
        IF NOT FOUND THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-invitation-mail-scope/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invitation_not_sendable')
            );
        END IF;
        IF v_user_id = v_initial.invited_by THEN
            v_inviter_role := v_user.role;
            v_inviter_active := v_user.is_active;
        END IF;
        IF v_user_id = v_owner_id THEN
            v_owner_role := v_user.role;
            v_owner_active := v_user.is_active;
        END IF;
    END LOOP;
    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(v_initial.email, 0)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'invitation email is busy';
    END IF;
    IF v_initial.project_id IS NOT NULL THEN
        v_project := aap_workspace_v1.lock_invitation_project(
            v_initial.project_id, v_owner_id
        );
        IF v_project->>'schema' IS DISTINCT FROM 'aap-workspace-invitation-project/v1'
           OR v_project->'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-invitation-mail-scope/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invitation_not_sendable')
            );
        END IF;
    END IF;
    BEGIN
        SELECT invitation.*
        INTO v_invitation
        FROM public.user_invitations AS invitation
        WHERE invitation.id = p_invitation_id
        FOR SHARE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RAISE;
    END;
    IF NOT FOUND OR v_invitation.invited_by IS DISTINCT FROM v_initial.invited_by
       OR v_invitation.project_id IS DISTINCT FROM v_initial.project_id
       OR pg_catalog.encode(
           pg_catalog.sha256(pg_catalog.convert_to(v_invitation.token, 'UTF8')), 'hex'
       ) IS DISTINCT FROM p_expected_token_hash THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-invitation-mail-scope/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invitation_superseded')
        );
    END IF;
    IF v_invitation.accepted_at IS NOT NULL OR v_invitation.revoked_at IS NOT NULL
       OR v_invitation.expires_at <= v_now
       OR v_inviter_role IS NULL OR NOT v_inviter_active
       OR v_inviter_role NOT IN ('super_admin','project_admin')
       OR (v_invitation.role IN ('super_admin','project_admin')
           AND v_inviter_role <> 'super_admin') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-invitation-mail-scope/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invitation_not_sendable')
        );
    END IF;
    IF v_invitation.project_id IS NOT NULL THEN
        v_member_role := COALESCE(
            v_invitation.project_role,
            CASE WHEN v_invitation.role IN ('annotator','reviewer','viewer')
                THEN v_invitation.role END
        );
        IF v_member_role NOT IN ('annotator','reviewer','viewer')
           OR v_owner_role IS NULL OR NOT v_owner_active
           OR v_owner_role NOT IN ('super_admin','project_admin')
           OR (v_inviter_role = 'project_admin'
               AND v_owner_id <> v_initial.invited_by) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-invitation-mail-scope/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invitation_not_sendable')
            );
        END IF;
    ELSE
        v_member_role := NULL;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-invitation-mail-scope/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'invitation_id', v_invitation.id,
            'token_hash', p_expected_token_hash,
            'email', v_invitation.email,
            'role', v_invitation.role,
            'project_id', v_invitation.project_id,
            'project_name', CASE WHEN v_invitation.project_id IS NULL THEN NULL
                                 ELSE v_project #>> '{data,name}' END,
            'project_member_role', v_member_role,
            'expires_at', v_invitation.expires_at,
            'invited_by', v_invitation.invited_by
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1.lock_invitation_mail_scope(uuid, text)
    FROM PUBLIC;
