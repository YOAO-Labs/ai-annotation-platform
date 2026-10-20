-- Native transactional ownership for every invitation read/write path.
CREATE FUNCTION aap_identity_invitations_v1._failure(
    p_code text, p_policy text DEFAULT NULL, p_detail text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-invitations-native/v1',
        'ok', false,
        'error', CASE WHEN p_code = 'policy_rejected'
            THEN pg_catalog.jsonb_build_object(
                'code', p_code, 'policy', p_policy, 'detail', p_detail
            )
            ELSE pg_catalog.jsonb_build_object('code', p_code) END
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._failure(text, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._strip_unicode_space(p_value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.btrim(
        COALESCE(p_value, ''),
        pg_catalog.chr(9) || pg_catalog.chr(10) || pg_catalog.chr(11)
        || pg_catalog.chr(12) || pg_catalog.chr(13) || pg_catalog.chr(28)
        || pg_catalog.chr(29) || pg_catalog.chr(30) || pg_catalog.chr(31)
        || pg_catalog.chr(32) || pg_catalog.chr(133) || pg_catalog.chr(160)
        || pg_catalog.chr(5760) || pg_catalog.chr(8192) || pg_catalog.chr(8193)
        || pg_catalog.chr(8194) || pg_catalog.chr(8195) || pg_catalog.chr(8196)
        || pg_catalog.chr(8197) || pg_catalog.chr(8198) || pg_catalog.chr(8199)
        || pg_catalog.chr(8200) || pg_catalog.chr(8201) || pg_catalog.chr(8202)
        || pg_catalog.chr(8232) || pg_catalog.chr(8233) || pg_catalog.chr(8239)
        || pg_catalog.chr(8287) || pg_catalog.chr(12288)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._strip_unicode_space(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._account_record(p_user_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', account.id,
        'email', account.email,
        'name', account.name,
        'role', account.role,
        'group_name', account.group_name,
        'group_id', account.group_id,
        'status', account.status,
        'is_active', account.is_active,
        'disabled_kind', account.disabled_kind,
        'disabled_at', account.disabled_at,
        'disabled_by', account.disabled_by,
        'disabled_reason', account.disabled_reason,
        'email_verified_at', account.email_verified_at,
        'last_login_at', account.last_login_at,
        'last_seen_at', account.last_seen_at,
        'password_admin_reset_at', account.password_admin_reset_at,
        'deactivation_requested_at', account.deactivation_requested_at,
        'deactivation_scheduled_at', account.deactivation_scheduled_at,
        'preferences', account.preferences,
        'avatar_ref', account.avatar_ref,
        'created_at', account.created_at
    )
    FROM public.users AS account
    WHERE account.id = p_user_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._account_record(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._invitation_record(
    p_invitation_id uuid, p_project_name text
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', invitation.id,
        'email', invitation.email,
        'role', invitation.role,
        'group_name', invitation.group_name,
        'project_id', invitation.project_id,
        'project_name', p_project_name,
        'project_member_role', CASE WHEN invitation.project_id IS NULL THEN NULL
            ELSE COALESCE(invitation.project_role,
                CASE WHEN invitation.role IN ('annotator','reviewer','viewer')
                     THEN invitation.role END) END,
        'status', CASE WHEN invitation.accepted_at IS NOT NULL THEN 'accepted'
            WHEN invitation.revoked_at IS NOT NULL THEN 'revoked'
            WHEN invitation.expires_at <= pg_catalog.statement_timestamp() THEN 'expired'
            ELSE 'pending' END,
        'expires_at', invitation.expires_at,
        'invited_by', invitation.invited_by,
        'invited_by_name', inviter.name,
        'accepted_at', invitation.accepted_at,
        'revoked_at', invitation.revoked_at,
        'created_at', invitation.created_at
    )
    FROM public.user_invitations AS invitation
    LEFT JOIN public.users AS inviter ON inviter.id = invitation.invited_by
    WHERE invitation.id = p_invitation_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._invitation_record(uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._project_map(p_project_ids uuid[])
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_ids uuid[];
    v_offset integer;
    v_reply jsonb;
    v_project jsonb;
    v_names jsonb := '{}'::jsonb;
BEGIN
    IF p_project_ids IS NULL THEN
        RETURN v_names;
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(ids.id ORDER BY ids.id), ARRAY[]::uuid[])
    INTO v_ids
    FROM (
        SELECT DISTINCT candidate.id
        FROM unnest(p_project_ids) AS candidate(id)
        WHERE candidate.id IS NOT NULL
    ) AS ids;
    IF pg_catalog.cardinality(v_ids) = 0 THEN RETURN v_names; END IF;
    v_offset := 1;
    WHILE v_offset <= pg_catalog.cardinality(v_ids) LOOP
        v_reply := aap_workspace_v1.invitation_projects(
            v_ids[v_offset:pg_catalog.least(v_offset + 499, pg_catalog.cardinality(v_ids))]
        );
        IF v_reply->>'schema' IS DISTINCT FROM 'aap-workspace-invitation-projects/v1'
           OR v_reply->'ok' IS DISTINCT FROM 'true'::jsonb
           OR pg_catalog.jsonb_typeof(v_reply #> '{data,projects}') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Workspace invitation project contract mismatch';
        END IF;
        FOR v_project IN SELECT value FROM pg_catalog.jsonb_array_elements(v_reply #> '{data,projects}')
        LOOP
            v_names := v_names || pg_catalog.jsonb_build_object(
                v_project->>'id', v_project->'name'
            );
        END LOOP;
        v_offset := v_offset + 500;
    END LOOP;
    RETURN v_names;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._project_map(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._policy(p_fallbacks jsonb)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_policy jsonb;
BEGIN
    v_policy := aap_settings_v1.invitation_policy(p_fallbacks);
    IF pg_catalog.jsonb_typeof(v_policy) IS DISTINCT FROM 'object'
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(v_policy)) <> 4
       OR NOT (v_policy ?& ARRAY[
           'max_invitations_per_day','invitation_ttl_days','frontend_base_url','expire_minutes'
       ])
       OR pg_catalog.jsonb_typeof(v_policy->'max_invitations_per_day') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_policy->'invitation_ttl_days') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_policy->'frontend_base_url') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(v_policy->>'frontend_base_url') < 1
       OR pg_catalog.jsonb_typeof(v_policy->'expire_minutes') IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Settings invitation policy contract mismatch';
    END IF;
    RETURN v_policy;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._policy(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._lock_users(p_user_ids uuid[])
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_user_id uuid;
    v_locked uuid;
BEGIN
    IF p_user_ids IS NULL OR pg_catalog.cardinality(p_user_ids) > 2000
       OR pg_catalog.array_position(p_user_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation User lock set';
    END IF;
    FOR v_user_id IN
        SELECT DISTINCT candidate.id
        FROM unnest(p_user_ids) AS candidate(id)
        ORDER BY candidate.id
    LOOP
        BEGIN
            SELECT account.id INTO v_locked
            FROM public.users AS account
            WHERE account.id = v_user_id
            FOR KEY SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN false;
        END;
        IF NOT FOUND THEN RETURN false; END IF;
    END LOOP;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._lock_users(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._actor(
    p_actor jsonb, p_expected_key_hash text, p_allow_null boolean, p_lock boolean
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
    v_count integer;
    v_actor_id uuid;
    v_credential_id uuid;
    v_kind text;
    v_generation bigint;
    v_expires_at timestamptz;
    v_user record;
    v_key record;
    v_principal jsonb;
BEGIN
    IF p_actor IS NULL OR pg_catalog.jsonb_typeof(p_actor) = 'null' THEN
        IF p_allow_null AND p_expected_key_hash IS NULL THEN
            RETURN pg_catalog.jsonb_build_object('ok', true, 'principal', NULL, 'actor', NULL);
        END IF;
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    IF NOT p_lock THEN
        IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_count
        FROM pg_catalog.jsonb_object_keys(p_actor);
        IF v_count <> 10 OR NOT (p_actor ?& ARRAY[
            'actor_id','name','email','role','is_active','credential_type',
            'credential_id','generation','scopes','expires_at'
        ]) OR EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(p_actor) AS field(name)
            WHERE field.name NOT IN (
                'actor_id','name','email','role','is_active','credential_type',
                'credential_id','generation','scopes','expires_at'
            )
        ) OR pg_catalog.jsonb_typeof(p_actor->'actor_id') IS DISTINCT FROM 'string'
          OR (p_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          OR pg_catalog.jsonb_typeof(p_actor->'name') IS DISTINCT FROM 'string'
          OR pg_catalog.jsonb_typeof(p_actor->'email') IS DISTINCT FROM 'string'
          OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
          OR p_actor->'is_active' IS DISTINCT FROM 'true'::jsonb
          OR pg_catalog.jsonb_typeof(p_actor->'credential_type') IS DISTINCT FROM 'string'
          OR p_actor->>'credential_type' NOT IN ('jwt','api_key')
          OR pg_catalog.jsonb_typeof(p_actor->'credential_id') IS DISTINCT FROM 'string'
          OR (p_actor->>'credential_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
        IF p_actor->>'credential_type' = 'jwt' THEN
            IF pg_catalog.jsonb_typeof(p_actor->'generation') IS DISTINCT FROM 'number'
               OR (p_actor->>'generation') !~ '^(0|[1-9][0-9]*)$'
               OR pg_catalog.jsonb_typeof(p_actor->'scopes') IS DISTINCT FROM 'null'
               OR pg_catalog.jsonb_typeof(p_actor->'expires_at') IS DISTINCT FROM 'string'
               OR (p_actor->>'expires_at') !~* '(z|[+-][0-9]{2}:[0-9]{2})$'
               OR p_expected_key_hash IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure('invalid_request');
            END IF;
        ELSE
            IF pg_catalog.jsonb_typeof(p_actor->'generation') IS DISTINCT FROM 'null'
               OR pg_catalog.jsonb_typeof(p_actor->'scopes') IS DISTINCT FROM 'array'
               OR EXISTS (
                   SELECT 1 FROM pg_catalog.jsonb_array_elements(p_actor->'scopes') AS scope(value)
                   WHERE pg_catalog.jsonb_typeof(scope.value) IS DISTINCT FROM 'string'
               ) OR pg_catalog.jsonb_typeof(p_actor->'expires_at') NOT IN ('null','string')
               OR (pg_catalog.jsonb_typeof(p_actor->'expires_at') = 'string'
                   AND (p_actor->>'expires_at') !~* '(z|[+-][0-9]{2}:[0-9]{2})$')
               OR p_expected_key_hash IS NULL
               OR pg_catalog.char_length(p_expected_key_hash) <> 60
               OR p_expected_key_hash !~ '^\$2[aby]\$12\$[./A-Za-z0-9]{53}$' THEN
                RETURN aap_identity_invitations_v1._failure('invalid_request');
            END IF;
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'ok', true, 'principal', p_actor,
            'actor_id', p_actor->>'actor_id', 'credential_type', p_actor->>'credential_type',
            'credential_id', p_actor->>'credential_id'
        );
    END IF;

    BEGIN
        v_actor_id := (p_actor->>'actor_id')::uuid;
        v_credential_id := (p_actor->>'credential_id')::uuid;
        IF p_actor->>'credential_type' = 'jwt' THEN
            v_generation := (p_actor->>'generation')::bigint;
            v_expires_at := (p_actor->>'expires_at')::timestamptz;
        END IF;
    EXCEPTION WHEN invalid_text_representation OR datetime_field_overflow
                 OR numeric_value_out_of_range THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END;
    v_kind := p_actor->>'credential_type';
    IF v_kind = 'jwt' AND (v_generation < 0 OR NOT pg_catalog.isfinite(v_expires_at)) THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;

    BEGIN
        SELECT account.id, account.name, account.email, account.role,
               account.is_active, account.credential_generation
        INTO v_user
        FROM public.users AS account
        WHERE account.id = v_actor_id
        FOR KEY SHARE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_invitations_v1._failure('credential_busy');
    END;
    IF NOT FOUND THEN RETURN aap_identity_invitations_v1._failure('credential_invalid'); END IF;
    IF NOT v_user.is_active THEN RETURN aap_identity_invitations_v1._failure('user_inactive'); END IF;
    IF v_user.role NOT IN ('super_admin','project_admin','employee','viewer') THEN
        RETURN aap_identity_invitations_v1._failure('invalid_account_role');
    END IF;

    IF v_kind = 'jwt' THEN
        IF v_user.credential_generation IS DISTINCT FROM v_generation THEN
            RETURN aap_identity_invitations_v1._failure('generation_outdated');
        END IF;
        IF v_expires_at <= pg_catalog.clock_timestamp() THEN
            RETURN aap_identity_invitations_v1._failure('token_expired');
        END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock_shared(
            pg_catalog.hashtextextended('aap-identity-jti:' || v_credential_id::text, 0)
        ) THEN RETURN aap_identity_invitations_v1._failure('credential_busy'); END IF;
        IF aap_identity_v1.is_jti_revoked(v_credential_id) THEN
            RETURN aap_identity_invitations_v1._failure('credential_revoked');
        END IF;
        v_principal := pg_catalog.jsonb_build_object(
            'actor_id', v_user.id, 'name', v_user.name, 'email', v_user.email,
            'role', v_user.role, 'is_active', true, 'credential_type', 'jwt',
            'credential_id', v_credential_id, 'generation', v_user.credential_generation,
            'scopes', NULL, 'expires_at', v_expires_at
        );
    ELSE
        BEGIN
            SELECT key.key_hash, key.scopes, key.expires_at
            INTO v_key
            FROM public.api_keys AS key
            WHERE key.id = v_credential_id AND key.user_id = v_actor_id
              AND key.revoked_at IS NULL
            FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('credential_busy');
        END;
        IF NOT FOUND THEN RETURN aap_identity_invitations_v1._failure('credential_revoked'); END IF;
        IF v_key.key_hash IS DISTINCT FROM p_expected_key_hash THEN
            RETURN aap_identity_invitations_v1._failure('credential_invalid');
        END IF;
        IF v_key.expires_at IS NOT NULL AND v_key.expires_at <= pg_catalog.clock_timestamp() THEN
            RETURN aap_identity_invitations_v1._failure('credential_revoked');
        END IF;
        BEGIN
            v_expires_at := CASE
                WHEN pg_catalog.jsonb_typeof(p_actor->'expires_at') = 'null' THEN NULL
                ELSE (p_actor->>'expires_at')::timestamptz END;
        EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format
                     OR datetime_field_overflow THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END;
        IF v_key.scopes IS DISTINCT FROM p_actor->'scopes'
           OR v_key.expires_at IS DISTINCT FROM v_expires_at THEN
            RETURN aap_identity_invitations_v1._failure('credential_invalid');
        END IF;
        v_principal := pg_catalog.jsonb_build_object(
            'actor_id', v_user.id, 'name', v_user.name, 'email', v_user.email,
            'role', v_user.role, 'is_active', true, 'credential_type', 'api_key',
            'credential_id', v_credential_id, 'generation', NULL,
            'scopes', v_key.scopes, 'expires_at', v_key.expires_at
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'ok', true, 'principal', v_principal,
        'actor', pg_catalog.jsonb_build_object(
            'id', v_user.id, 'email', v_user.email, 'name', v_user.name,
            'role', v_user.role, 'is_active', v_user.is_active,
            'credential_generation', v_user.credential_generation
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._actor(jsonb, text, boolean, boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1.invitation_policy()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-invitation-policy/v1',
        'policy', 'aap-identity-invitations/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1.invitation_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1.invitation_quota_reached(
    p_actor_id uuid, p_now timestamptz, p_limit integer
)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p_actor_id IS NOT NULL AND p_now IS NOT NULL
       AND pg_catalog.isfinite(p_now) AND p_limit BETWEEN 1 AND 1000
       AND (SELECT pg_catalog.count(*) FROM public.user_invitations AS invitation
            WHERE invitation.invited_by = p_actor_id
              AND invitation.created_at >= p_now - INTERVAL '24 hours') >= p_limit
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1.invitation_quota_reached(
    uuid, timestamptz, integer
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._empty_result()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'invitation', NULL, 'invitations', NULL, 'items', NULL,
        'total', NULL, 'page', NULL, 'page_size', NULL, 'pages', NULL,
        'stats', NULL, 'account', NULL, 'claims', NULL, 'acceptance', NULL,
        'email_verification_required', NULL, 'preview', NULL, 'succeeded', NULL,
        'failed', NULL, 'exported_by', NULL, 'exported_at', NULL,
        'invitation_id', NULL, 'email', NULL, 'mail_status', NULL,
        'mail_failure_code', NULL, 'frontend_base_url', NULL
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._empty_result() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._incompatible_role_detail(
    p_account_role text, p_project_role text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT '当前账号是' || CASE p_account_role
        WHEN 'super_admin' THEN '超级管理员'
        WHEN 'project_admin' THEN '项目管理员'
        WHEN 'employee' THEN '员工'
        WHEN 'viewer' THEN '观察者'
        WHEN 'annotator' THEN '标注员'
        WHEN 'reviewer' THEN '审核员'
        ELSE p_account_role END
        || '，无法持有' || CASE p_project_role
            WHEN 'annotator' THEN '标注员'
            WHEN 'reviewer' THEN '审核员'
            WHEN 'viewer' THEN '观察者'
            ELSE p_project_role END
        || '项目职责。为避免静默修改平台角色，请联系管理员调整账号角色后再接受邀请'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._incompatible_role_detail(text, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._args_valid(
    p_operation text, p_args jsonb
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count integer;
    v_item jsonb;
    v_index integer;
    v_seen integer[] := ARRAY[]::integer[];
BEGIN
    IF pg_catalog.jsonb_typeof(p_args) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_args);
    IF p_operation = 'create_invitation' THEN
        RETURN v_count = 5 AND p_args ?& ARRAY[
            'email','role','group_name','project_id','project_member_role'
        ] AND pg_catalog.jsonb_typeof(p_args->'email') = 'string'
          AND pg_catalog.char_length(p_args->>'email') BETWEEN 3 AND 255
          AND p_args->>'email' = pg_catalog.lower(aap_identity_invitations_v1._strip_unicode_space(p_args->>'email'))
          AND pg_catalog.jsonb_typeof(p_args->'role') = 'string'
          AND pg_catalog.jsonb_typeof(p_args->'group_name') IN ('string','null')
          AND (pg_catalog.jsonb_typeof(p_args->'group_name') = 'null'
               OR (p_args->>'group_name' = aap_identity_invitations_v1._strip_unicode_space(p_args->>'group_name')
                   AND pg_catalog.char_length(p_args->>'group_name') BETWEEN 1 AND 100))
          AND pg_catalog.jsonb_typeof(p_args->'project_id') IN ('string','null')
          AND (pg_catalog.jsonb_typeof(p_args->'project_id') = 'null'
               OR (p_args->>'project_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
          AND pg_catalog.jsonb_typeof(p_args->'project_member_role') IN ('string','null');
    ELSIF p_operation IN ('preview_invitation_batch','apply_invitation_batch') THEN
        IF v_count <> 1 OR NOT (p_args ? 'items')
           OR pg_catalog.jsonb_typeof(p_args->'items') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_args->'items') > 500 THEN RETURN false; END IF;
        FOR v_item IN SELECT value FROM pg_catalog.jsonb_array_elements(p_args->'items')
        LOOP
            SELECT pg_catalog.count(*)::integer INTO v_count
            FROM pg_catalog.jsonb_object_keys(v_item);
            IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object'
               OR (p_operation = 'preview_invitation_batch' AND v_count <> 8)
               OR (p_operation = 'apply_invitation_batch' AND v_count <> 6)
               OR NOT (v_item ?& ARRAY[
                   'index','email','role','group_name','project_id','project_member_role'
               ]) OR (p_operation = 'preview_invitation_batch'
                   AND NOT (v_item ?& ARRAY['invitation_id','token']))
               OR (p_operation = 'apply_invitation_batch'
                   AND (v_item ? 'invitation_id' OR v_item ? 'token'))
               OR pg_catalog.jsonb_typeof(v_item->'index') IS DISTINCT FROM 'number'
               OR (v_item->>'index') !~ '^(0|[1-9][0-9]*)$'
               OR (v_item->>'index')::numeric NOT BETWEEN 0 AND 499
               OR pg_catalog.jsonb_typeof(v_item->'email') IS DISTINCT FROM 'string'
               OR pg_catalog.char_length(v_item->>'email') NOT BETWEEN 3 AND 255
               OR v_item->>'email' IS DISTINCT FROM pg_catalog.lower(
                   aap_identity_invitations_v1._strip_unicode_space(v_item->>'email')
               ) OR pg_catalog.jsonb_typeof(v_item->'role') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_item->'group_name') NOT IN ('string','null')
               OR (pg_catalog.jsonb_typeof(v_item->'group_name') = 'string'
                   AND (v_item->>'group_name' IS DISTINCT FROM
                        aap_identity_invitations_v1._strip_unicode_space(v_item->>'group_name')
                        OR pg_catalog.char_length(v_item->>'group_name') NOT BETWEEN 1 AND 100))
               OR pg_catalog.jsonb_typeof(v_item->'project_id') NOT IN ('string','null')
               OR (pg_catalog.jsonb_typeof(v_item->'project_id') = 'string'
                   AND (v_item->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR pg_catalog.jsonb_typeof(v_item->'project_member_role') NOT IN ('string','null') THEN
                RETURN false;
            END IF;
            v_index := (v_item->>'index')::integer;
            IF v_index = ANY(v_seen) THEN RETURN false; END IF;
            v_seen := pg_catalog.array_append(v_seen, v_index);
            IF p_operation = 'preview_invitation_batch' AND (
                pg_catalog.jsonb_typeof(v_item->'invitation_id') IS DISTINCT FROM 'string'
                OR (v_item->>'invitation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                OR pg_catalog.jsonb_typeof(v_item->'token') IS DISTINCT FROM 'string'
                OR pg_catalog.char_length(v_item->>'token') NOT BETWEEN 1 AND 64
            ) THEN RETURN false; END IF;
        END LOOP;
        RETURN true;
    ELSIF p_operation = 'list_invitations' THEN
        RETURN v_count = 2 AND p_args ?& ARRAY['status','scope']
          AND pg_catalog.jsonb_typeof(p_args->'status') IS NOT DISTINCT FROM 'string'
          AND pg_catalog.jsonb_typeof(p_args->'scope') IS NOT DISTINCT FROM 'string'
          AND p_args->>'status' IN ('all','pending','accepted','expired','revoked')
          AND p_args->>'scope' IN ('me','all');
    ELSIF p_operation IN ('query_invitations','invitation_stats','export_invitations') THEN
        IF p_operation = 'query_invitations' THEN
            IF v_count <> 10 OR NOT (p_args ?& ARRAY[
                'page','page_size','status','scope','project_id','role','email',
                'search','created_from','created_to'
            ]) OR pg_catalog.jsonb_typeof(p_args->'page') IS DISTINCT FROM 'number'
              OR (p_args->>'page') !~ '^[1-9][0-9]*$'
              OR (p_args->>'page')::numeric > 2147483647
              OR pg_catalog.jsonb_typeof(p_args->'page_size') IS DISTINCT FROM 'number'
              OR (p_args->>'page_size') !~ '^[1-9][0-9]*$'
              OR (p_args->>'page_size')::numeric NOT BETWEEN 1 AND 500 THEN RETURN false; END IF;
        ELSIF p_operation = 'invitation_stats' THEN
            IF v_count <> 8 OR NOT (p_args ?& ARRAY[
                'status','scope','project_id','role','email','search','created_from','created_to'
            ]) THEN RETURN false; END IF;
        ELSE
            IF v_count <> 9 OR NOT (p_args ?& ARRAY[
                'format','status','scope','project_id','role','email','search','created_from','created_to'
            ]) OR pg_catalog.jsonb_typeof(p_args->'format') IS DISTINCT FROM 'string'
              OR p_args->>'format' NOT IN ('csv','json') THEN RETURN false; END IF;
        END IF;
        IF pg_catalog.jsonb_typeof(p_args->'status') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_args->'scope') IS DISTINCT FROM 'string'
           OR p_args->>'status' NOT IN ('all','pending','accepted','expired','revoked')
           OR p_args->>'scope' NOT IN ('me','all')
           OR pg_catalog.jsonb_typeof(p_args->'project_id') NOT IN ('string','null')
           OR (pg_catalog.jsonb_typeof(p_args->'project_id') = 'string'
               AND (p_args->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(p_args->'role') NOT IN ('string','null')
           OR pg_catalog.jsonb_typeof(p_args->'email') NOT IN ('string','null')
           OR pg_catalog.jsonb_typeof(p_args->'search') NOT IN ('string','null')
           OR pg_catalog.jsonb_typeof(p_args->'created_from') NOT IN ('string','null')
           OR pg_catalog.jsonb_typeof(p_args->'created_to') NOT IN ('string','null') THEN RETURN false; END IF;
        RETURN true;
    ELSIF p_operation IN ('revoke_invitation','resend_invitation','send_invitation_email') THEN
        RETURN v_count = 1 AND p_args ? 'invitation_id'
          AND pg_catalog.jsonb_typeof(p_args->'invitation_id') = 'string'
          AND (p_args->>'invitation_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    ELSIF p_operation IN ('resolve_invitation','accept_invitation') THEN
        RETURN v_count = 1 AND p_args ? 'invitation_token'
          AND pg_catalog.jsonb_typeof(p_args->'invitation_token') = 'string'
          AND pg_catalog.char_length(p_args->>'invitation_token') BETWEEN 1 AND 64;
    ELSIF p_operation = 'register_invitation' THEN
        RETURN v_count = 2 AND p_args ?& ARRAY['invitation_token','name']
          AND pg_catalog.jsonb_typeof(p_args->'invitation_token') = 'string'
          AND pg_catalog.char_length(p_args->>'invitation_token') BETWEEN 1 AND 64
          AND pg_catalog.jsonb_typeof(p_args->'name') = 'string'
          AND pg_catalog.char_length(p_args->>'name') BETWEEN 1 AND 100
          AND p_args->>'name' = aap_identity_invitations_v1._strip_unicode_space(p_args->>'name');
    END IF;
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._args_valid(text, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._read_result(
    p_kind text, p_actor_id uuid, p_args jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_now timestamptz := pg_catalog.statement_timestamp();
    v_status text := p_args->>'status';
    v_scope text := p_args->>'scope';
    v_project_id uuid;
    v_role text := NULLIF(p_args->>'role', '');
    v_email text := aap_identity_invitations_v1._strip_unicode_space(p_args->>'email');
    v_search text := aap_identity_invitations_v1._strip_unicode_space(p_args->>'search');
    v_created_from timestamptz;
    v_created_to timestamptz;
    v_total bigint;
    v_pending bigint;
    v_accepted bigint;
    v_expired bigint;
    v_revoked bigint;
    v_page integer := CASE WHEN p_kind = 'list_invitations' THEN 1
                           ELSE (p_args->>'page')::integer END;
    v_page_size integer := CASE WHEN p_kind = 'list_invitations' THEN NULL
                                ELSE (p_args->>'page_size')::integer END;
    v_offset bigint := 0;
    v_ids uuid[];
    v_project_ids uuid[];
    v_project_names jsonb;
    v_rows jsonb := '[]'::jsonb;
    v_id uuid;
    v_invitation record;
    v_result jsonb := aap_identity_invitations_v1._empty_result();
BEGIN
    IF p_kind NOT IN (
        'list_invitations','query_invitations','invitation_stats','export_invitations'
    ) OR p_actor_id IS NULL THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    IF pg_catalog.jsonb_typeof(p_args->'project_id') = 'string' THEN
        v_project_id := (p_args->>'project_id')::uuid;
    END IF;
    IF pg_catalog.jsonb_typeof(p_args->'created_from') = 'string' THEN
        BEGIN v_created_from := (p_args->>'created_from')::timestamptz;
        EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format
                     OR datetime_field_overflow THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END;
        IF NOT pg_catalog.isfinite(v_created_from) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    END IF;
    IF pg_catalog.jsonb_typeof(p_args->'created_to') = 'string' THEN
        BEGIN v_created_to := (p_args->>'created_to')::timestamptz;
        EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format
                     OR datetime_field_overflow THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END;
        IF NOT pg_catalog.isfinite(v_created_to) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    END IF;
    IF v_page_size IS NOT NULL THEN
        v_offset := (v_page::bigint - 1) * v_page_size::bigint;
    END IF;

    WITH filtered AS MATERIALIZED (
        SELECT invitation.*,
               CASE WHEN invitation.accepted_at IS NOT NULL THEN 'accepted'
                    WHEN invitation.revoked_at IS NOT NULL THEN 'revoked'
                    WHEN invitation.expires_at <= v_now THEN 'expired'
                    ELSE 'pending' END AS effective_status
        FROM public.user_invitations AS invitation
        WHERE (v_scope = 'all' OR invitation.invited_by = p_actor_id)
          AND (v_project_id IS NULL OR invitation.project_id = v_project_id)
          AND (v_role IS NULL OR invitation.role = v_role)
          AND (v_email IS NULL OR v_email = ''
               OR invitation.email ILIKE '%' || v_email || '%')
          AND (v_search IS NULL OR v_search = ''
               OR invitation.email ILIKE '%' || v_search || '%'
               OR invitation.group_name ILIKE '%' || v_search || '%')
          AND (v_created_from IS NULL OR invitation.created_at >= v_created_from)
          AND (v_created_to IS NULL OR invitation.created_at <= v_created_to)
    )
    SELECT pg_catalog.count(*)::bigint,
           pg_catalog.count(*) FILTER (WHERE filtered.effective_status = 'pending')::bigint,
           pg_catalog.count(*) FILTER (WHERE filtered.effective_status = 'accepted')::bigint,
           pg_catalog.count(*) FILTER (WHERE filtered.effective_status = 'expired')::bigint,
           pg_catalog.count(*) FILTER (WHERE filtered.effective_status = 'revoked')::bigint
    INTO v_total, v_pending, v_accepted, v_expired, v_revoked
    FROM filtered
    WHERE v_status = 'all' OR filtered.effective_status = v_status;

    IF p_kind = 'invitation_stats' THEN
        v_result := v_result || pg_catalog.jsonb_build_object(
            'stats', pg_catalog.jsonb_build_object(
                'total', v_total, 'pending', v_pending, 'accepted', v_accepted,
                'expired', v_expired, 'revoked', v_revoked
            )
        );
        RETURN v_result;
    END IF;

    WITH filtered AS MATERIALIZED (
        SELECT invitation.*,
               CASE WHEN invitation.accepted_at IS NOT NULL THEN 'accepted'
                    WHEN invitation.revoked_at IS NOT NULL THEN 'revoked'
                    WHEN invitation.expires_at <= v_now THEN 'expired'
                    ELSE 'pending' END AS effective_status
        FROM public.user_invitations AS invitation
        WHERE (v_scope = 'all' OR invitation.invited_by = p_actor_id)
          AND (v_project_id IS NULL OR invitation.project_id = v_project_id)
          AND (v_role IS NULL OR invitation.role = v_role)
          AND (v_email IS NULL OR v_email = ''
               OR invitation.email ILIKE '%' || v_email || '%')
          AND (v_search IS NULL OR v_search = ''
               OR invitation.email ILIKE '%' || v_search || '%'
               OR invitation.group_name ILIKE '%' || v_search || '%')
          AND (v_created_from IS NULL OR invitation.created_at >= v_created_from)
          AND (v_created_to IS NULL OR invitation.created_at <= v_created_to)
    )
    SELECT COALESCE(
        pg_catalog.array_agg(selected.id ORDER BY selected.created_at DESC, selected.id DESC),
        ARRAY[]::uuid[]
    ) INTO v_ids
    FROM (
        SELECT filtered.id, filtered.created_at
        FROM filtered
        WHERE v_status = 'all' OR filtered.effective_status = v_status
        ORDER BY filtered.created_at DESC, filtered.id DESC
        OFFSET CASE WHEN p_kind = 'query_invitations' THEN v_offset ELSE 0 END
        LIMIT CASE WHEN p_kind = 'query_invitations' THEN v_page_size ELSE NULL END
    ) AS selected;

    SELECT COALESCE(pg_catalog.array_agg(invitation.project_id), ARRAY[]::uuid[])
    INTO v_project_ids
    FROM public.user_invitations AS invitation
    WHERE invitation.id = ANY(v_ids) AND invitation.project_id IS NOT NULL;
    v_project_names := aap_identity_invitations_v1._project_map(v_project_ids);
    FOREACH v_id IN ARRAY v_ids LOOP
        SELECT invitation.id, invitation.project_id
        INTO v_invitation
        FROM public.user_invitations AS invitation
        WHERE invitation.id = v_id;
        v_rows := v_rows || pg_catalog.jsonb_build_array(
            aap_identity_invitations_v1._invitation_record(
                v_id,
                CASE WHEN v_invitation.project_id IS NULL THEN NULL
                     ELSE v_project_names->>(v_invitation.project_id::text) END
            )
        );
    END LOOP;
    v_result := v_result || pg_catalog.jsonb_build_object('invitations', v_rows);
    IF p_kind = 'query_invitations' THEN
        v_result := v_result || pg_catalog.jsonb_build_object(
            'total', v_total, 'page', v_page, 'page_size', v_page_size,
            'pages', CASE WHEN v_total = 0 THEN 0
                          ELSE pg_catalog.ceil(v_total::numeric / v_page_size)::integer END
        );
    ELSIF p_kind = 'export_invitations' THEN
        v_result := v_result || pg_catalog.jsonb_build_object(
            'exported_by', (SELECT account.email FROM public.users AS account
                            WHERE account.id = p_actor_id),
            'exported_at', v_now
        );
    END IF;
    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._read_result(text, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._native_actor(p_user_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN account.id IS NULL THEN NULL ELSE pg_catalog.jsonb_build_object(
        'id', account.id, 'email', account.email, 'name', account.name,
        'role', account.role, 'is_active', account.is_active,
        'credential_generation', account.credential_generation
    ) END
    FROM (SELECT p_user_id AS id) AS requested
    LEFT JOIN public.users AS account ON account.id = requested.id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._native_actor(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._project_record(p_project_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
BEGIN
    IF p_project_id IS NULL THEN RETURN NULL; END IF;
    v_reply := aap_workspace_v1.invitation_projects(ARRAY[p_project_id]);
    IF v_reply->>'schema' IS DISTINCT FROM 'aap-workspace-invitation-projects/v1'
       OR v_reply->'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_reply #> '{data,projects}') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Workspace invitation project contract mismatch';
    END IF;
    IF pg_catalog.jsonb_array_length(v_reply #> '{data,projects}') = 0 THEN
        RETURN NULL;
    END IF;
    RETURN v_reply #> '{data,projects,0}';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._project_record(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._batch_snapshot(
    p_actor_id uuid, p_items jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item jsonb;
    v_index text;
    v_project_id uuid;
    v_target_id uuid;
    v_project jsonb;
    v_users uuid[] := ARRAY[p_actor_id]::uuid[];
    v_owners jsonb := '{}'::jsonb;
    v_targets jsonb := '{}'::jsonb;
BEGIN
    IF p_actor_id IS NULL OR pg_catalog.jsonb_typeof(p_items) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_items) > 500 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation batch snapshot';
    END IF;
    FOR v_item IN SELECT value FROM pg_catalog.jsonb_array_elements(p_items)
    LOOP
        v_index := v_item->>'index';
        IF pg_catalog.jsonb_typeof(v_item->'project_id') = 'string' THEN
            v_project_id := (v_item->>'project_id')::uuid;
            v_project := aap_identity_invitations_v1._project_record(v_project_id);
            IF v_project IS NOT NULL THEN
                v_users := pg_catalog.array_append(v_users,(v_project->>'owner_id')::uuid);
                v_owners := v_owners || pg_catalog.jsonb_build_object(
                    v_index,v_project->'owner_id'
                );
            ELSE
                v_owners := v_owners || pg_catalog.jsonb_build_object(v_index,NULL);
            END IF;
        ELSE
            v_project_id := NULL;
            v_owners := v_owners || pg_catalog.jsonb_build_object(v_index,NULL);
        END IF;
        SELECT account.id INTO v_target_id
        FROM public.users AS account
        WHERE account.email = v_item->>'email' AND account.is_active
        ORDER BY account.id LIMIT 1;
        IF FOUND THEN v_users := pg_catalog.array_append(v_users,v_target_id); END IF;
        v_targets := v_targets || pg_catalog.jsonb_build_object(v_index,v_target_id);
    END LOOP;
    RETURN pg_catalog.jsonb_build_object(
        'user_ids',COALESCE((
            SELECT pg_catalog.jsonb_agg(user_id::text ORDER BY user_id)
            FROM (SELECT DISTINCT unnest(v_users) AS user_id) AS ids
        ),'[]'::jsonb),
        'owners',v_owners,'targets',v_targets
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._batch_snapshot(uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1.prepare_invitation_command(p_command jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_count integer;
    v_operation text;
    v_operation_id uuid;
    v_fingerprint text;
    v_derivation_id text;
    v_actor_input jsonb;
    v_expected_key_hash text;
    v_context jsonb;
    v_config jsonb;
    v_args jsonb;
    v_policy jsonb;
    v_parsed_actor jsonb;
    v_actor_lock jsonb;
    v_principal jsonb;
    v_actor_id uuid;
    v_actor_role text;
    v_actor_native jsonb;
    v_allow_null boolean;
    v_user_ids uuid[] := ARRAY[]::uuid[];
    v_invitation record;
    v_initial record;
    v_target record;
    v_owner record;
    v_inviter record;
    v_project jsonb;
    v_workspace jsonb;
    v_project_id uuid;
    v_owner_id uuid;
    v_target_id uuid;
    v_snapshot_target_id uuid;
    v_token text;
    v_project_role text;
    v_operation_lock boolean;
    v_receipt record;
    v_outbox record;
    v_replayed boolean := false;
    v_result jsonb;
    v_batch_snapshot jsonb;
    v_batch_result jsonb;
    v_audit_id bigint;
    v_detail jsonb;
    v_data jsonb := pg_catalog.jsonb_build_object(
        'policy', NULL, 'actor', NULL, 'invitation', NULL, 'project', NULL,
        'target', NULL, 'membership_exists', NULL, 'existing_password_hash', NULL,
        'invitation_token', NULL, 'mail_token_hash', NULL,
        'mail_token_ciphertext', NULL, 'mail_encryption_id', NULL,
        'mail_frontend_url', NULL, 'replayed', false, 'result', NULL
    );
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_command);
    IF v_count <> 10 OR NOT (p_command ?& ARRAY[
        'schema','operation','operation_id','actor','expected_key_hash',
        'request_fingerprint','derivation_id','audit_context','config','args'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_command) AS field(name)
        WHERE field.name NOT IN (
            'schema','operation','operation_id','actor','expected_key_hash',
            'request_fingerprint','derivation_id','audit_context','config','args'
        )
    ) OR p_command->>'schema' IS DISTINCT FROM 'aap-identity-invitations-native/v1'
      OR pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string'
      OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
      OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
      OR (p_command->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
      OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
      OR (p_command->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
      OR pg_catalog.jsonb_typeof(p_command->'actor') NOT IN ('object','null')
      OR pg_catalog.jsonb_typeof(p_command->'expected_key_hash') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(p_command->'audit_context') IS DISTINCT FROM 'object'
      OR pg_catalog.jsonb_typeof(p_command->'config') IS DISTINCT FROM 'object'
      OR pg_catalog.jsonb_typeof(p_command->'args') IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    v_operation := p_command->>'operation';
    IF v_operation NOT IN (
        'create_invitation','preview_invitation_batch','apply_invitation_batch',
        'list_invitations','query_invitations','invitation_stats','export_invitations',
        'revoke_invitation','resend_invitation','send_invitation_email',
        'resolve_invitation','register_invitation','accept_invitation'
    ) OR NOT aap_identity_invitations_v1._args_valid(v_operation, p_command->'args') THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    BEGIN
        v_operation_id := (p_command->>'operation_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END;
    v_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';
    v_actor_input := p_command->'actor';
    v_expected_key_hash := p_command->>'expected_key_hash';
    v_context := p_command->'audit_context';
    v_config := p_command->'config';
    v_args := p_command->'args';
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(v_context);
    IF v_count <> 4 OR NOT (v_context ?& ARRAY['method','path','ip','request_id'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(v_context) AS field(name)
           WHERE field.name NOT IN ('method','path','ip','request_id')
       ) OR pg_catalog.jsonb_typeof(v_context->'method') NOT IN ('string','null')
       OR pg_catalog.jsonb_typeof(v_context->'path') NOT IN ('string','null')
       OR pg_catalog.jsonb_typeof(v_context->'ip') NOT IN ('string','null')
       OR pg_catalog.jsonb_typeof(v_context->'request_id') NOT IN ('string','null')
       OR NOT aap_audit_v1.valid_request_context(v_context) THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(v_config);
    IF v_count <> 4 OR NOT (v_config ?& ARRAY[
        'max_invitations_per_day','invitation_ttl_days','frontend_base_url','expire_minutes'
    ]) OR pg_catalog.jsonb_typeof(v_config->'max_invitations_per_day') IS DISTINCT FROM 'number'
      OR (v_config->>'max_invitations_per_day') !~ '^[1-9][0-9]*$'
      OR (v_config->>'max_invitations_per_day')::numeric NOT BETWEEN 1 AND 1000
      OR pg_catalog.jsonb_typeof(v_config->'invitation_ttl_days') IS DISTINCT FROM 'number'
      OR (v_config->>'invitation_ttl_days') !~ '^[1-9][0-9]*$'
      OR (v_config->>'invitation_ttl_days')::numeric NOT BETWEEN 1 AND 90
      OR pg_catalog.jsonb_typeof(v_config->'frontend_base_url') IS DISTINCT FROM 'string'
      OR pg_catalog.char_length(v_config->>'frontend_base_url') < 1
      OR pg_catalog.jsonb_typeof(v_config->'expire_minutes') IS DISTINCT FROM 'number'
      OR (v_config->>'expire_minutes') !~ '^[1-9][0-9]*$'
      OR (v_config->>'expire_minutes')::numeric NOT BETWEEN 1 AND 525600 THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    v_policy := aap_identity_invitations_v1._policy(v_config);
    v_data := v_data || pg_catalog.jsonb_build_object('policy', v_policy);

    v_allow_null := v_operation IN ('resolve_invitation','register_invitation');
    v_parsed_actor := aap_identity_invitations_v1._actor(
        v_actor_input, v_expected_key_hash, v_allow_null, false
    );
    IF v_parsed_actor->'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_parsed_actor; END IF;
    IF v_actor_input IS NOT NULL AND pg_catalog.jsonb_typeof(v_actor_input) <> 'null' THEN
        BEGIN v_actor_id := (v_actor_input->>'actor_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END;
    ELSIF NOT v_allow_null THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;

    -- Resolve invitation and target snapshots before taking any locks, so all
    -- participating Users can be fenced in UUID order before email/Project/row.
    IF v_operation IN ('resolve_invitation','register_invitation','accept_invitation') THEN
        v_token := v_args->>'invitation_token';
        SELECT invitation.* INTO v_initial
        FROM public.user_invitations AS invitation
        WHERE invitation.token = v_token;
        IF NOT FOUND THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请链接无效'
            );
        END IF;
        v_project_id := v_initial.project_id;
        v_project := aap_identity_invitations_v1._project_record(v_project_id);
        IF v_project_id IS NOT NULL AND v_project IS NULL THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','project_not_found','目标项目已删除，无法接受邀请'
            );
        END IF;
        v_owner_id := (v_project->>'owner_id')::uuid;
        v_target_id := CASE WHEN v_operation = 'accept_invitation' THEN v_actor_id
                            ELSE NULL END;
        IF v_operation = 'register_invitation' THEN
            SELECT account.id INTO v_target_id
            FROM public.users AS account
            WHERE account.email = v_initial.email AND account.is_active
            ORDER BY account.id LIMIT 1;
        END IF;
        v_user_ids := ARRAY[v_initial.invited_by, v_owner_id, v_target_id, v_actor_id];
    ELSIF v_operation = 'create_invitation' THEN
        IF pg_catalog.jsonb_typeof(v_args->'project_id') = 'string' THEN
            v_project_id := (v_args->>'project_id')::uuid;
            v_project := aap_identity_invitations_v1._project_record(v_project_id);
            IF v_project IS NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_not_found','目标项目不存在'
                );
            END IF;
            v_owner_id := (v_project->>'owner_id')::uuid;
        END IF;
        SELECT account.id, account.role, account.is_active
        INTO v_target
        FROM public.users AS account
        WHERE account.email = v_args->>'email' AND account.is_active
        ORDER BY account.id LIMIT 1;
        IF FOUND THEN
            v_target_id := v_target.id;
            v_snapshot_target_id := v_target.id;
        END IF;
        v_user_ids := ARRAY[v_actor_id, v_owner_id, v_target_id];
    ELSIF v_operation IN ('preview_invitation_batch','apply_invitation_batch') THEN
        v_batch_snapshot := aap_identity_invitations_v1._batch_snapshot(
            v_actor_id,v_args->'items'
        );
        SELECT COALESCE(
            pg_catalog.array_agg((item.value #>> '{}')::uuid), ARRAY[]::uuid[]
        ) INTO v_user_ids
        FROM pg_catalog.jsonb_array_elements(v_batch_snapshot->'user_ids') AS item(value);
    ELSIF v_operation IN ('revoke_invitation','resend_invitation','send_invitation_email') THEN
        BEGIN v_project_id := NULL; v_owner_id := NULL; END;
        SELECT invitation.* INTO v_initial
        FROM public.user_invitations AS invitation
        WHERE invitation.id = (v_args->>'invitation_id')::uuid;
        IF NOT FOUND THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        IF v_operation = 'send_invitation_email' AND v_initial.project_id IS NOT NULL THEN
            v_project_id := v_initial.project_id;
            v_project := aap_identity_invitations_v1._project_record(v_project_id);
            IF v_project IS NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_not_found','目标项目已删除，无法接受邀请'
                );
            END IF;
            v_owner_id := (v_project->>'owner_id')::uuid;
        END IF;
        v_user_ids := ARRAY[v_actor_id, v_owner_id,
            CASE WHEN v_operation = 'send_invitation_email' THEN v_initial.invited_by END];
    ELSE
        v_user_ids := ARRAY[v_actor_id];
    END IF;
    v_user_ids := ARRAY(SELECT DISTINCT id FROM unnest(v_user_ids) AS users(id)
                        WHERE id IS NOT NULL ORDER BY id);
    IF NOT aap_identity_invitations_v1._lock_users(v_user_ids) THEN
        RETURN aap_identity_invitations_v1._failure('current_scope_busy');
    END IF;
    v_actor_lock := aap_identity_invitations_v1._actor(
        v_actor_input, v_expected_key_hash, v_allow_null, true
    );
    IF v_actor_lock->'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_actor_lock; END IF;
    v_principal := v_actor_lock->'principal';
    v_actor_native := v_actor_lock->'actor';
    v_actor_role := v_actor_native->>'role';
    IF v_actor_id IS NOT NULL AND v_operation <> 'accept_invitation'
       AND v_actor_role NOT IN ('super_admin','project_admin') THEN
        RETURN aap_identity_invitations_v1._failure(
            'policy_rejected','manager_required','当前账号不再具备邀请权限'
        );
    END IF;
    v_data := v_data || pg_catalog.jsonb_build_object('actor', v_actor_native);

    IF v_operation = 'preview_invitation_batch' THEN
        v_operation_lock := pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-identity-invitation-operation:' || v_operation_id::text,0)
        );
        IF NOT v_operation_lock THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        BEGIN
            PERFORM account.id FROM public.users AS account
            WHERE account.id = v_actor_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        v_batch_result := aap_identity_invitations_v1._run_invitation_preview(
            v_actor_id,v_actor_role,v_principal->>'email',v_context,v_policy,
            v_args->'items',v_batch_snapshot
        );
        v_data := v_data || pg_catalog.jsonb_build_object('result',v_batch_result);
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',v_data
        );
    ELSIF v_operation = 'apply_invitation_batch' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',v_data
        );
    END IF;

    IF v_operation IN ('list_invitations','query_invitations','invitation_stats','export_invitations') THEN
        IF v_args->>'scope' = 'all' AND v_actor_role <> 'super_admin' THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','scope_all_forbidden','仅超级管理员可查看全部邀请'
            );
        END IF;
        v_result := aap_identity_invitations_v1._read_result(
            v_operation, v_actor_id, v_args
        );
        IF v_result->'ok' = 'false'::jsonb THEN RETURN v_result; END IF;
        IF v_operation = 'export_invitations' THEN
            IF NOT pg_catalog.pg_try_advisory_xact_lock(
                pg_catalog.hashtextextended('aap-identity-invitation-operation:' || v_operation_id::text, 0)
            ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
            SELECT * INTO v_receipt FROM public.identity_invitation_operation_receipts AS receipt
            WHERE receipt.operation_id = v_operation_id;
            IF FOUND THEN
                IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
                   OR v_receipt.operation IS DISTINCT FROM v_operation
                   OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
                   OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                v_result := v_receipt.result;
                v_replayed := true;
            ELSE
                v_detail := pg_catalog.jsonb_build_object(
                    'format', v_args->>'format',
                    'count', pg_catalog.jsonb_array_length(v_result->'invitations'),
                    'scope', v_args->>'scope', 'project_id', v_args->'project_id',
                    'actor_email', v_principal->>'email',
                    'ip', v_context->'ip', 'request_id', v_context->'request_id'
                );
                v_audit_id := aap_audit_v1.record_identity_invitation_event(
                    'invitation.export',
                    pg_catalog.jsonb_build_object(
                        'actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role
                    ),
                    pg_catalog.jsonb_build_object('type','invitation','id',NULL),
                    v_context, v_detail
                );
                INSERT INTO public.identity_invitation_operation_receipts (
                    operation_id, actor_id, operation, request_fingerprint,
                    derivation_id, result, audit_id
                ) VALUES (
                    v_operation_id, v_actor_id, v_operation, v_fingerprint,
                    v_derivation_id, v_result, v_audit_id
                );
            END IF;
        END IF;
        v_data := v_data || pg_catalog.jsonb_build_object(
            'replayed', v_replayed, 'result', v_result
        );
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',v_data
        );
    END IF;

    IF v_operation = 'create_invitation' THEN
        IF v_args->>'role' NOT IN ('super_admin','project_admin','employee','viewer') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invalid_platform_role','非法角色: ' || (v_args->>'role')
            );
        END IF;
        IF v_project_id IS NULL AND v_args->'project_member_role' <> 'null'::jsonb THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','account_project_role','账号级邀请不得包含项目职责'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_args->>'project_member_role' NOT IN (
            'annotator','reviewer','viewer'
        ) THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invalid_project_role',
                '目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_args->>'role' = 'viewer'
           AND v_args->>'project_member_role' <> 'viewer' THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','platform_project_role_incompatible',
                '账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_args->>'role' IN ('super_admin','project_admin') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','account_project_role','管理员账号不能通过项目邀请加入项目'
            );
        END IF;
        IF v_actor_role = 'project_admin' AND v_args->>'role' NOT IN ('employee','viewer') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','project_admin_role_forbidden','项目管理员仅能邀请员工或观察者'
            );
        END IF;
        BEGIN
            PERFORM account.id FROM public.users AS account
            WHERE account.id = v_actor_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        v_operation_lock := pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-identity-invitation-operation:' || v_operation_id::text,0)
        );
        IF NOT v_operation_lock THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(v_args->>'email', 0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        IF v_project_id IS NOT NULL THEN
            v_workspace := aap_workspace_v1.lock_invitation_project(v_project_id, v_owner_id);
            IF v_workspace->>'schema' IS DISTINCT FROM 'aap-workspace-invitation-project/v1' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Workspace invitation project contract mismatch';
            END IF;
            IF v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                IF v_workspace #>> '{error,code}' = 'project_missing' THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','project_not_found','目标项目不存在'
                    );
                END IF;
                RETURN aap_identity_invitations_v1._failure('current_scope_busy');
            END IF;
            SELECT account.id,account.role,account.is_active INTO v_owner
            FROM public.users AS account WHERE account.id = v_owner_id;
            IF NOT FOUND OR NOT v_owner.is_active
               OR v_owner.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目当前没有可用的负责人'
                );
            END IF;
            IF v_actor_role = 'project_admin' AND v_owner_id <> v_actor_id THEN
                RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','project_admin_scope','邀请人已不再管理目标项目'
                );
            END IF;
        END IF;
        SELECT * INTO v_receipt
        FROM public.identity_invitation_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_invitation_id := (v_receipt.result #>> '{invitation,id}')::uuid;
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_invitation_id
              AND invitation.invited_by = v_actor_id
              AND invitation.email = v_args->>'email'
            FOR KEY SHARE NOWAIT;
            IF NOT FOUND OR v_invitation.project_id IS DISTINCT FROM v_project_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_data := v_data || pg_catalog.jsonb_build_object(
                'invitation',aap_identity_invitations_v1._invitation_record(
                    v_invitation.id,CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END
                ),'project',v_project,'replayed',true,'result',v_receipt.result
            );
            RETURN pg_catalog.jsonb_build_object(
                'schema','aap-identity-invitations-native/v1','ok',true,
                'operation',v_operation,'operation_id',v_operation_id,
                'principal',v_principal,'data',v_data
            );
        END IF;
        IF aap_identity_invitations_v1.invitation_quota_reached(
            v_actor_id,v_now,(v_policy->>'max_invitations_per_day')::integer
        ) THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','daily_quota',
                '每位管理员滚动 24 小时最多邀请 ' ||
                (v_policy->>'max_invitations_per_day') || ' 人，请稍后再试'
            );
        END IF;
        IF v_project_id IS NOT NULL THEN
            SELECT account.id,account.role,account.is_active INTO v_target
            FROM public.users AS account
            WHERE account.email = v_args->>'email' AND account.is_active
            ORDER BY account.id LIMIT 1;
            IF FOUND THEN
                IF v_target.id IS DISTINCT FROM v_snapshot_target_id THEN
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END IF;
                v_target_id := v_target.id;
                IF v_target.role NOT IN ('employee','viewer')
                   OR (v_target.role = 'viewer' AND v_args->>'project_member_role' <> 'viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','incompatible_account_role',
                        aap_identity_invitations_v1._incompatible_role_detail(
                            v_target.role,v_args->>'project_member_role'
                        )
                    );
                END IF;
                IF aap_workspace_v1.read_invitation_membership(v_project_id,v_target_id) THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','already_member','该账号已是目标项目成员'
                    );
                END IF;
                v_data := v_data || pg_catalog.jsonb_build_object(
                    'target', aap_identity_invitations_v1._native_actor(v_target_id),
                    'membership_exists', false
                );
            ELSE
                IF v_snapshot_target_id IS NOT NULL THEN
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END IF;
                v_target_id := NULL;
            END IF;
            v_data := v_data || pg_catalog.jsonb_build_object('project',
                pg_catalog.jsonb_build_object(
                    'id',v_project_id,'name',v_workspace #>> '{data,name}',
                    'owner_id',v_owner_id
                )
            );
        ELSIF EXISTS (
            SELECT 1 FROM public.users AS account
            WHERE account.email = v_args->>'email' AND account.is_active
        ) THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','active_email_exists',
                '邮箱 ' || (v_args->>'email') || ' 已注册，请选择目标项目后邀请该用户加入'
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',v_data
        );
    END IF;

    IF v_operation IN ('resolve_invitation','register_invitation','accept_invitation') THEN
        IF v_operation IN ('register_invitation','accept_invitation') THEN
            v_operation_lock := pg_catalog.pg_try_advisory_xact_lock(
                pg_catalog.hashtextextended('aap-identity-invitation-operation:' || v_operation_id::text, 0)
            );
            IF NOT v_operation_lock THEN
                RETURN aap_identity_invitations_v1._failure('current_scope_busy');
            END IF;
        END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(v_initial.email, 0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        IF v_project_id IS NOT NULL THEN
            v_workspace := aap_workspace_v1.lock_invitation_project(v_project_id,v_owner_id);
            IF v_workspace->>'schema' IS DISTINCT FROM 'aap-workspace-invitation-project/v1' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Workspace invitation project contract mismatch';
            END IF;
            IF v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                IF v_workspace #>> '{error,code}' = 'project_missing' THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','project_not_found','目标项目已删除，无法接受邀请'
                    );
                END IF;
                RETURN aap_identity_invitations_v1._failure('current_scope_busy');
            END IF;
            v_project := pg_catalog.jsonb_build_object(
                'id',v_project_id,'name',v_workspace #>> '{data,name}',
                'owner_id',v_owner_id
            );
            SELECT account.id,account.role,account.is_active INTO v_owner
            FROM public.users AS account WHERE account.id = v_owner_id;
        END IF;
        BEGIN
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_initial.id
            FOR KEY SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        IF NOT FOUND OR v_invitation.token IS DISTINCT FROM v_token THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请链接无效'
            );
        END IF;
        IF v_invitation.accepted_at IS NOT NULL THEN
            IF v_operation = 'register_invitation'
               AND v_invitation.accepted_user_id = v_operation_id THEN
                SELECT * INTO v_receipt
                FROM public.identity_invitation_operation_receipts AS receipt
                WHERE receipt.operation_id = v_operation_id;
                IF NOT FOUND OR v_receipt.operation <> v_operation
                   OR v_receipt.actor_id IS NOT NULL
                   OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
                   OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                SELECT account.password_hash INTO v_target
                FROM public.users AS account
                WHERE account.id = v_operation_id AND account.is_active
                  AND account.email = v_invitation.email;
                IF NOT FOUND THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','actor_inactive','账号已停用，请联系管理员'
                    );
                END IF;
                v_data := v_data || pg_catalog.jsonb_build_object(
                    'existing_password_hash',v_target.password_hash,
                    'invitation',aap_identity_invitations_v1._invitation_record(
                        v_invitation.id,CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END
                    ),'project',v_project,'replayed',true,'result',v_receipt.result
                );
                RETURN pg_catalog.jsonb_build_object(
                    'schema','aap-identity-invitations-native/v1','ok',true,
                    'operation',v_operation,'operation_id',v_operation_id,
                    'principal',v_principal,'data',v_data
                );
            ELSIF v_operation = 'accept_invitation' THEN
                SELECT * INTO v_receipt
                FROM public.identity_invitation_operation_receipts AS receipt
                WHERE receipt.operation_id = v_operation_id;
                IF FOUND THEN
                    IF v_receipt.operation <> v_operation
                       OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
                       OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
                       OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id
                       OR v_invitation.accepted_user_id IS DISTINCT FROM v_actor_id THEN
                        RETURN aap_identity_invitations_v1._failure('operation_conflict');
                    END IF;
                    v_data := v_data || pg_catalog.jsonb_build_object(
                        'invitation',aap_identity_invitations_v1._invitation_record(
                            v_invitation.id,CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END
                        ),'project',v_project,'target',v_actor_native,
                        'replayed',true,'result',v_receipt.result
                    );
                    RETURN pg_catalog.jsonb_build_object(
                        'schema','aap-identity-invitations-native/v1','ok',true,
                        'operation',v_operation,'operation_id',v_operation_id,
                        'principal',v_principal,'data',v_data
                    );
                END IF;
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_used','该邀请已被使用'
                );
            ELSE
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_used','该邀请已被使用'
                );
            END IF;
        END IF;
        IF v_invitation.revoked_at IS NOT NULL THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_revoked','该邀请已撤销'
            );
        END IF;
        IF v_invitation.expires_at <= v_now THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_expired','该邀请已过期'
            );
        END IF;
        SELECT account.id,account.name,account.email,account.role,account.is_active
        INTO v_inviter FROM public.users AS account WHERE account.id = v_invitation.invited_by;
        IF NOT FOUND OR NOT v_inviter.is_active
           OR v_inviter.role NOT IN ('super_admin','project_admin') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','issuer_inactive','邀请人已停用或不再具备管理权限'
            );
        END IF;
        IF v_invitation.role IN ('super_admin','project_admin') AND v_inviter.role <> 'super_admin' THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','issuer_role_invalid','该邀请权限已失效'
            );
        END IF;
        IF v_project_id IS NOT NULL AND (
            v_owner.id IS NULL OR NOT v_owner.is_active
            OR v_owner.role NOT IN ('super_admin','project_admin')
        ) THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','project_owner_unavailable','目标项目当前没有可用的负责人'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_inviter.role = 'project_admin'
           AND v_owner_id <> v_invitation.invited_by THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','project_admin_scope','邀请人已不再管理目标项目'
            );
        END IF;
        v_project_role := CASE WHEN v_project_id IS NULL THEN NULL
            ELSE COALESCE(v_invitation.project_role,
                CASE WHEN v_invitation.role IN ('annotator','reviewer','viewer')
                     THEN v_invitation.role END) END;
        IF v_operation = 'register_invitation'
           AND v_invitation.role NOT IN ('super_admin','project_admin','employee','viewer') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','legacy_platform_role','该邀请包含已失效的账号角色，请联系管理员重发'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_project_role NOT IN ('annotator','reviewer','viewer') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invalid_project_role',
                '目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_invitation.role IN ('super_admin','project_admin') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','account_project_role','管理员账号不能通过项目邀请加入项目'
            );
        END IF;
        IF v_operation = 'register_invitation' THEN
            IF pg_catalog.char_length(COALESCE(v_invitation.group_name,'')) > 100 THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','group_name_invalid','邀请中的数据组名称无效，请联系管理员重发'
                );
            END IF;
            SELECT account.id INTO v_target
            FROM public.users AS account
            WHERE account.email = v_invitation.email AND account.is_active;
            IF FOUND THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','email_exists','该邮箱已注册，请让该用户登录后确认项目邀请'
                );
            END IF;
        ELSIF v_operation = 'accept_invitation' THEN
            IF v_project_id IS NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','account_only_invitation',
                    '该邀请仅用于创建新账号，已有账号只能接受带目标项目的邀请'
                );
            END IF;
            IF v_actor_native->>'email' IS DISTINCT FROM v_invitation.email THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','email_mismatch','当前登录邮箱与邀请邮箱不一致，请使用被邀请邮箱登录'
                );
            END IF;
            IF v_actor_role NOT IN ('employee','viewer')
               OR (v_actor_role = 'viewer' AND v_project_role <> 'viewer') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','incompatible_account_role',
                    aap_identity_invitations_v1._incompatible_role_detail(
                        v_actor_role,v_project_role
                    )
                );
            END IF;
            IF aap_workspace_v1.read_invitation_membership(v_project_id,v_actor_id) THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','already_member','该账号已是目标项目成员'
                );
            END IF;
        END IF;
        v_data := v_data || pg_catalog.jsonb_build_object(
            'invitation',aap_identity_invitations_v1._invitation_record(
                v_invitation.id,CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END
            ),
            'project',v_project,
            'target',CASE WHEN v_operation = 'accept_invitation'
                          THEN v_actor_native ELSE NULL END
        );
        IF v_operation = 'resolve_invitation' THEN
            v_result := aap_identity_invitations_v1._empty_result()
                || pg_catalog.jsonb_build_object(
                    'invitation',aap_identity_invitations_v1._invitation_record(
                        v_invitation.id,CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END
                    )
                );
            v_data := v_data || pg_catalog.jsonb_build_object('result',v_result);
        ELSIF v_operation = 'register_invitation'
           AND EXISTS (
               SELECT 1 FROM public.identity_invitation_operation_receipts AS receipt
               WHERE receipt.operation_id = v_operation_id
           ) THEN
            RETURN aap_identity_invitations_v1._failure('operation_conflict');
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',v_data
        );
    END IF;

    IF v_operation IN ('revoke_invitation','resend_invitation','send_invitation_email') THEN
        IF v_actor_role = 'project_admin'
           AND v_initial.invited_by IS DISTINCT FROM v_actor_id THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        IF v_operation IN ('revoke_invitation','resend_invitation') THEN
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_initial.id
            FOR KEY SHARE NOWAIT;
            IF NOT FOUND THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_not_found','邀请不存在'
                );
            END IF;
            SELECT * INTO v_receipt
            FROM public.identity_invitation_operation_receipts AS receipt
            WHERE receipt.operation_id = v_operation_id;
            IF FOUND THEN
                IF v_receipt.operation IS DISTINCT FROM v_operation
                   OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
                   OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
                   OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                v_data := v_data || pg_catalog.jsonb_build_object(
                    'invitation',aap_identity_invitations_v1._invitation_record(
                        v_invitation.id, NULL
                    ),'replayed',true,'result',v_receipt.result
                );
            ELSE
                IF v_invitation.accepted_at IS NOT NULL THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','invitation_accepted','该邀请已被接受，无法操作'
                    );
                END IF;
                IF v_operation = 'resend_invitation'
                   AND v_actor_role = 'project_admin'
                   AND v_invitation.role NOT IN ('employee','viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','privileged_resend','项目管理员不能重发高权限邀请'
                    );
                END IF;
                v_data := v_data || pg_catalog.jsonb_build_object(
                    'invitation',aap_identity_invitations_v1._invitation_record(
                        v_invitation.id,NULL
                    )
                );
            END IF;
        ELSE
            IF NOT pg_catalog.pg_try_advisory_xact_lock(
                pg_catalog.hashtextextended(v_initial.email, 0)
            ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
            IF v_project_id IS NOT NULL THEN
                v_workspace := aap_workspace_v1.lock_invitation_project(v_project_id,v_owner_id);
                IF v_workspace->>'schema' IS DISTINCT FROM 'aap-workspace-invitation-project/v1' THEN
                    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Workspace invitation project contract mismatch';
                END IF;
                IF v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','project_not_found','目标项目已删除，无法接受邀请'
                    );
                END IF;
                v_project := pg_catalog.jsonb_build_object(
                    'id',v_project_id,'name',v_workspace #>> '{data,name}',
                    'owner_id',v_owner_id
                );
                SELECT account.id,account.role,account.is_active INTO v_owner
                FROM public.users AS account WHERE account.id = v_owner_id;
            END IF;
            BEGIN
                SELECT invitation.* INTO v_invitation
                FROM public.user_invitations AS invitation
                WHERE invitation.id = v_initial.id
                FOR KEY SHARE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_identity_invitations_v1._failure('current_scope_busy');
            END;
            IF NOT FOUND THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_not_found','邀请不存在'
                );
            END IF;
            IF v_invitation.accepted_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_used','该邀请已被使用'
                );
            ELSIF v_invitation.revoked_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_revoked','该邀请已撤销'
                );
            ELSIF v_invitation.expires_at <= v_now THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_expired','该邀请已过期'
                );
            END IF;
            SELECT account.id,account.role,account.is_active INTO v_inviter
            FROM public.users AS account WHERE account.id = v_invitation.invited_by;
            IF NOT FOUND OR NOT v_inviter.is_active
               OR v_inviter.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','issuer_inactive','邀请人已停用或不再具备管理权限'
                );
            END IF;
            IF v_invitation.role IN ('super_admin','project_admin') AND v_inviter.role <> 'super_admin' THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','issuer_role_invalid','该邀请权限已失效'
                );
            END IF;
            IF v_project_id IS NOT NULL AND (
                v_owner.id IS NULL OR NOT v_owner.is_active
                OR v_owner.role NOT IN ('super_admin','project_admin')
            ) THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目当前没有可用的负责人'
                );
            END IF;
            IF v_project_id IS NOT NULL AND v_inviter.role = 'project_admin'
               AND v_owner_id <> v_invitation.invited_by THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_admin_scope','邀请人已不再管理目标项目'
                );
            END IF;
            v_project_role := CASE WHEN v_project_id IS NULL THEN NULL
                ELSE COALESCE(v_invitation.project_role,
                    CASE WHEN v_invitation.role IN ('annotator','reviewer','viewer')
                         THEN v_invitation.role END) END;
            IF v_project_id IS NOT NULL AND v_project_role NOT IN ('annotator','reviewer','viewer') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invalid_project_role',
                    '目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色'
                );
            END IF;
            SELECT * INTO v_receipt
            FROM public.identity_invitation_operation_receipts AS receipt
            WHERE receipt.operation_id = v_operation_id;
            IF FOUND THEN
                IF v_receipt.operation IS DISTINCT FROM v_operation
                   OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
                   OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
                   OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                SELECT outbox.* INTO v_outbox
                FROM public.identity_credential_mail_outbox AS outbox
                WHERE outbox.id = v_operation_id
                  AND outbox.purpose = 'invitation'
                  AND outbox.invitation_id = v_invitation.id;
                IF NOT FOUND OR v_outbox.token_hash <> pg_catalog.encode(
                    pg_catalog.sha256(pg_catalog.convert_to(v_invitation.token,'UTF8')),'hex'
                ) THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                v_result := v_receipt.result || pg_catalog.jsonb_build_object(
                    'mail_status',CASE WHEN v_outbox.status = 'delivered' THEN 'sent'
                        WHEN v_outbox.status IN ('rejected','cancelled','expired') THEN 'failed'
                        ELSE 'pending' END,
                    'mail_failure_code',CASE WHEN v_outbox.status = 'delivered' THEN NULL
                        ELSE v_outbox.last_reason END
                );
                v_data := v_data || pg_catalog.jsonb_build_object(
                    'invitation',aap_identity_invitations_v1._invitation_record(
                        v_invitation.id,CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END
                    ),'project',v_project,'mail_token_hash',v_outbox.token_hash,
                    'mail_token_ciphertext',v_outbox.token_ciphertext,
                    'mail_encryption_id',v_outbox.encryption_id,
                    'mail_frontend_url',v_outbox.frontend_base_url,
                    'replayed',true,'result',v_result
                );
            ELSE
                v_data := v_data || pg_catalog.jsonb_build_object(
                    'invitation',aap_identity_invitations_v1._invitation_record(
                        v_invitation.id,CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END
                    ),'project',v_project,'invitation_token',v_invitation.token,
                    'mail_frontend_url',v_policy->>'frontend_base_url'
                );
            END IF;
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',v_data
        );
    END IF;

    -- Remaining operation-specific preparation is below.
    RETURN aap_identity_invitations_v1._failure('unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1.prepare_invitation_command(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._batch_effects(
    p_preview boolean,
    p_actor_id uuid,
    p_actor_role text,
    p_actor_email text,
    p_context jsonb,
    p_policy jsonb,
    p_items jsonb,
    p_batch_tokens jsonb,
    p_snapshot jsonb
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
    v_item jsonb;
    v_item_index integer;
    v_token_item jsonb;
    v_email text;
    v_role text;
    v_group_name text;
    v_project_id uuid;
    v_project_role text;
    v_project jsonb;
    v_workspace jsonb;
    v_owner_id uuid;
    v_expected_target_id uuid;
    v_owner record;
    v_target record;
    v_target_id uuid;
    v_invitation_id uuid;
    v_token text;
    v_now timestamptz;
    v_failure text;
    v_project_name text;
    v_audit_id bigint;
    v_items jsonb := '[]'::jsonb;
    v_seen_emails text[] := ARRAY[]::text[];
    v_succeeded integer := 0;
    v_failed integer := 0;
    v_result jsonb;
BEGIN
    IF p_preview IS NULL OR p_actor_id IS NULL OR p_actor_role NOT IN ('super_admin','project_admin')
       OR pg_catalog.jsonb_typeof(p_items) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(p_snapshot) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_snapshot->'owners') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_snapshot->'targets') IS DISTINCT FROM 'object'
       OR (p_preview AND p_batch_tokens IS NOT NULL)
       OR (NOT p_preview AND pg_catalog.jsonb_typeof(p_batch_tokens) IS DISTINCT FROM 'array') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation batch helper request';
    END IF;
    IF NOT p_preview AND pg_catalog.jsonb_array_length(p_batch_tokens) <> pg_catalog.jsonb_array_length(p_items) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invitation batch token count mismatch';
    END IF;

    FOR v_item IN
        SELECT value FROM pg_catalog.jsonb_array_elements(p_items) AS item(value)
        ORDER BY (value->>'index')::integer
    LOOP
        v_now := pg_catalog.clock_timestamp();
        v_item_index := (v_item->>'index')::integer;
        v_email := v_item->>'email';
        v_role := v_item->>'role';
        v_group_name := v_item->>'group_name';
        v_project_id := CASE WHEN pg_catalog.jsonb_typeof(v_item->'project_id') = 'string'
                             THEN (v_item->>'project_id')::uuid END;
        v_project_role := v_item->>'project_member_role';
        v_owner_id := CASE WHEN v_project_id IS NOT NULL
            AND pg_catalog.jsonb_typeof(p_snapshot #> ARRAY['owners',v_item_index::text]) = 'string'
            THEN (p_snapshot #>> ARRAY['owners',v_item_index::text])::uuid END;
        v_expected_target_id := CASE
            WHEN pg_catalog.jsonb_typeof(p_snapshot #> ARRAY['targets',v_item_index::text]) = 'string'
            THEN (p_snapshot #>> ARRAY['targets',v_item_index::text])::uuid END;
        v_project_name := NULL;
        v_failure := NULL;
        v_target_id := NULL;
        v_invitation_id := NULL;
        v_token := NULL;

        IF v_email = ANY(v_seen_emails) THEN
            v_failure := '本批次中邮箱重复';
        ELSIF v_role NOT IN ('super_admin','project_admin','employee','viewer') THEN
            v_failure := '非法角色: ' || v_role;
        ELSIF v_project_id IS NULL AND v_project_role IS NOT NULL THEN
            v_failure := '账号级邀请不得包含项目职责';
        ELSIF v_project_id IS NOT NULL AND v_project_role NOT IN ('annotator','reviewer','viewer') THEN
            v_failure := '目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色';
        ELSIF v_project_id IS NOT NULL AND v_role = 'viewer' AND v_project_role <> 'viewer' THEN
            v_failure := '账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目';
        ELSIF v_project_id IS NOT NULL AND v_role IN ('super_admin','project_admin') THEN
            v_failure := '管理员账号不能通过项目邀请加入项目';
        ELSIF p_actor_role = 'project_admin' AND v_role NOT IN ('employee','viewer') THEN
            v_failure := '项目管理员仅能邀请员工或观察者';
        ELSIF aap_identity_invitations_v1.invitation_quota_reached(
            p_actor_id,v_now,(p_policy->>'max_invitations_per_day')::integer
        ) THEN
            v_failure := '每位管理员滚动 24 小时最多邀请 ' ||
                (p_policy->>'max_invitations_per_day') || ' 人，请稍后再试';
        END IF;

        IF v_failure IS NULL AND NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(v_email,0)
        ) THEN
            v_failure := '处理失败，请稍后重试';
        END IF;
        IF v_failure IS NULL THEN
            BEGIN
                IF v_project_id IS NOT NULL AND v_owner_id IS NULL THEN
                    v_failure := '目标项目不存在';
                ELSIF v_project_id IS NOT NULL THEN
                    v_workspace := aap_workspace_v1.lock_invitation_project(
                        v_project_id,v_owner_id
                    );
                    IF v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                        v_failure := CASE WHEN v_workspace #>> '{error,code}' = 'project_missing'
                            THEN '目标项目不存在' ELSE '处理失败，请稍后重试' END;
                    ELSE
                        v_project_name := v_workspace #>> '{data,name}';
                        SELECT account.id,account.role,account.is_active INTO v_owner
                        FROM public.users AS account WHERE account.id = v_owner_id;
                        IF NOT FOUND OR NOT v_owner.is_active
                           OR v_owner.role NOT IN ('super_admin','project_admin') THEN
                            v_failure := '目标项目当前没有可用的负责人';
                        ELSIF p_actor_role = 'project_admin' AND v_owner_id <> p_actor_id THEN
                            v_failure := '邀请人已不再管理目标项目';
                        END IF;
                    END IF;
                END IF;

                IF v_failure IS NULL THEN
                    SELECT account.id,account.role INTO v_target
                    FROM public.users AS account
                    WHERE account.email = v_email AND account.is_active
                    ORDER BY account.id LIMIT 1;
                    IF FOUND THEN
                        v_target_id := v_target.id;
                        IF v_target_id IS DISTINCT FROM v_expected_target_id THEN
                            v_failure := '处理失败，请稍后重试';
                        ELSIF v_project_id IS NULL THEN
                            v_failure := '邮箱 ' || v_email || ' 已注册，请选择目标项目后邀请该用户加入';
                        ELSIF v_target.role NOT IN ('employee','viewer')
                              OR (v_target.role = 'viewer' AND v_project_role <> 'viewer') THEN
                            v_failure := aap_identity_invitations_v1._incompatible_role_detail(
                                v_target.role,v_project_role
                            );
                        ELSIF aap_workspace_v1.read_invitation_membership(v_project_id,v_target_id) THEN
                            v_failure := '该账号已是目标项目成员';
                        END IF;
                    ELSIF v_expected_target_id IS NOT NULL THEN
                        v_failure := '处理失败，请稍后重试';
                    END IF;
                END IF;

                IF v_failure IS NULL THEN
                    FOR v_target IN
                        SELECT invitation.id FROM public.user_invitations AS invitation
                        WHERE invitation.email = v_email AND invitation.accepted_at IS NULL
                          AND (p_actor_role = 'super_admin' OR invitation.invited_by = p_actor_id)
                        ORDER BY invitation.id FOR UPDATE NOWAIT
                    LOOP
                        UPDATE public.user_invitations AS invitation
                        SET expires_at = v_now WHERE invitation.id = v_target.id;
                    END LOOP;
                    IF p_preview THEN
                        v_invitation_id := (v_item->>'invitation_id')::uuid;
                        v_token := v_item->>'token';
                    ELSE
                        SELECT value INTO v_token_item
                        FROM pg_catalog.jsonb_array_elements(p_batch_tokens) AS item(value)
                        WHERE (value->>'index')::integer = v_item_index;
                        IF NOT FOUND OR (SELECT pg_catalog.count(*)
                            FROM pg_catalog.jsonb_object_keys(v_token_item)) <> 3
                           OR NOT (v_token_item ?& ARRAY['index','invitation_id','token'])
                           OR (v_token_item->>'index')::integer <> v_item_index
                           OR (v_token_item->>'invitation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.char_length(v_token_item->>'token') NOT BETWEEN 1 AND 64 THEN
                            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid invitation batch token item';
                        END IF;
                        v_invitation_id := (v_token_item->>'invitation_id')::uuid;
                        v_token := v_token_item->>'token';
                    END IF;
                    INSERT INTO public.user_invitations (
                        id,email,role,project_role,group_name,project_id,token,expires_at,invited_by
                    ) VALUES (
                        v_invitation_id,v_email,v_role,v_project_role,v_group_name,v_project_id,
                        v_token,v_now + pg_catalog.make_interval(days => (p_policy->>'invitation_ttl_days')::integer),
                        p_actor_id
                    );
                    v_audit_id := aap_audit_v1.record_identity_invitation_event(
                        'user.bulk_invite',
                        pg_catalog.jsonb_build_object(
                            'actor_id',p_actor_id,'email',p_actor_email,'role',p_actor_role
                        ),
                        pg_catalog.jsonb_build_object('type','invitation','id',v_invitation_id::text),
                        p_context,
                        pg_catalog.jsonb_build_object(
                            'email',v_email,'role',v_role,'project_id',v_project_id,
                            'preview',p_preview
                        )
                    );
                END IF;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                v_failure := '处理失败，请稍后重试';
            WHEN unique_violation THEN
                v_failure := '处理失败，请稍后重试';
            END;
        END IF;

        IF v_failure IS NULL THEN
            v_succeeded := v_succeeded + 1;
            v_seen_emails := pg_catalog.array_append(v_seen_emails,v_email);
            v_items := v_items || pg_catalog.jsonb_build_array(
                pg_catalog.jsonb_build_object(
                    'index',v_item_index,'email',v_email,'ok',true,'retryable',false,
                    'invitation_id',v_invitation_id,'project_id',v_project_id,
                    'project_name',v_project_name,'error',NULL
                )
            );
        ELSE
            v_failed := v_failed + 1;
            v_items := v_items || pg_catalog.jsonb_build_array(
                pg_catalog.jsonb_build_object(
                    'index',v_item_index,'email',v_email,'ok',false,'retryable',true,
                    'invitation_id',NULL,'project_id',NULL,'project_name',NULL,'error',v_failure
                )
            );
        END IF;
    END LOOP;
    RETURN aap_identity_invitations_v1._empty_result() || pg_catalog.jsonb_build_object(
        'items',v_items,'preview',p_preview,'succeeded',v_succeeded,'failed',v_failed,
        'frontend_base_url',p_policy->'frontend_base_url'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._batch_effects(
    boolean,uuid,text,text,jsonb,jsonb,jsonb,jsonb,jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1._run_invitation_preview(
    p_actor_id uuid,
    p_actor_role text,
    p_actor_email text,
    p_context jsonb,
    p_policy jsonb,
    p_items jsonb,
    p_snapshot jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_result jsonb;
BEGIN
    BEGIN
        v_result := aap_identity_invitations_v1._batch_effects(
            true,p_actor_id,p_actor_role,p_actor_email,p_context,p_policy,p_items,NULL,p_snapshot
        );
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'AAP_INVITATION_PREVIEW_ROLLBACK';
    EXCEPTION WHEN SQLSTATE 'P0001' THEN
        IF SQLERRM <> 'AAP_INVITATION_PREVIEW_ROLLBACK' THEN RAISE; END IF;
    END;
    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1._run_invitation_preview(
    uuid,text,text,jsonb,jsonb,jsonb,jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1.apply_invitation_command(p_command jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_count integer;
    v_operation text;
    v_operation_id uuid;
    v_fingerprint text;
    v_derivation_id text;
    v_actor_input jsonb;
    v_expected_key_hash text;
    v_context jsonb;
    v_config jsonb;
    v_args jsonb;
    v_derived jsonb;
    v_policy jsonb;
    v_actor_parse jsonb;
    v_actor_lock jsonb;
    v_principal jsonb;
    v_actor_native jsonb;
    v_actor_id uuid;
    v_actor_role text;
    v_allow_null boolean;
    v_user_ids uuid[] := ARRAY[]::uuid[];
    v_initial record;
    v_invitation record;
    v_owner record;
    v_inviter record;
    v_target record;
    v_project jsonb;
    v_workspace jsonb;
    v_project_id uuid;
    v_owner_id uuid;
    v_target_id uuid;
    v_snapshot_target_id uuid;
    v_project_role text;
    v_group jsonb;
    v_password_hash text;
    v_claims jsonb;
    v_group_id uuid;
    v_group_name text;
    v_user_id uuid;
    v_invitation_id uuid;
    v_mail_hash text;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_token text;
    v_status text;
    v_receipt record;
    v_outbox record;
    v_result jsonb;
    v_data jsonb;
    v_acceptance jsonb;
    v_batch jsonb;
    v_batch_item jsonb;
    v_scope_project record;
    v_batch_result jsonb;
    v_batch_snapshot jsonb;
    v_mail_context jsonb;
    v_audit_id bigint;
    v_detail jsonb;
    v_member_reply jsonb;
    v_active_count bigint;
    v_replayed boolean := false;
    v_operation_lock boolean;
BEGIN
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_command);
    IF v_count <> 11 OR NOT (p_command ?& ARRAY[
        'schema','operation','operation_id','actor','expected_key_hash',
        'request_fingerprint','derivation_id','audit_context','config','args','derived'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_command) AS field(name)
        WHERE field.name NOT IN (
            'schema','operation','operation_id','actor','expected_key_hash',
            'request_fingerprint','derivation_id','audit_context','config','args','derived'
        )
    ) OR p_command->>'schema' IS DISTINCT FROM 'aap-identity-invitations-native/v1'
      OR pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string'
      OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
      OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
      OR (p_command->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
      OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
      OR (p_command->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
      OR pg_catalog.jsonb_typeof(p_command->'actor') NOT IN ('object','null')
      OR pg_catalog.jsonb_typeof(p_command->'expected_key_hash') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(p_command->'audit_context') IS DISTINCT FROM 'object'
      OR pg_catalog.jsonb_typeof(p_command->'config') IS DISTINCT FROM 'object'
      OR pg_catalog.jsonb_typeof(p_command->'args') IS DISTINCT FROM 'object'
      OR pg_catalog.jsonb_typeof(p_command->'derived') IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    v_operation := p_command->>'operation';
    IF v_operation NOT IN (
        'create_invitation','apply_invitation_batch','revoke_invitation',
        'resend_invitation','send_invitation_email','register_invitation','accept_invitation'
    ) OR NOT aap_identity_invitations_v1._args_valid(v_operation,p_command->'args') THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    BEGIN v_operation_id := (p_command->>'operation_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END;
    v_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';
    v_actor_input := p_command->'actor';
    v_expected_key_hash := p_command->>'expected_key_hash';
    v_context := p_command->'audit_context';
    v_config := p_command->'config';
    v_args := p_command->'args';
    v_derived := p_command->'derived';
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(v_derived);
    IF v_count <> 9 OR NOT (v_derived ?& ARRAY[
        'password_hash','claims','token','batch_tokens','mail_token_hash',
        'mail_token_ciphertext','mail_encryption_id','frontend_base_url','mail_frontend_url'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(v_derived) AS field(name)
        WHERE field.name NOT IN (
            'password_hash','claims','token','batch_tokens','mail_token_hash',
            'mail_token_ciphertext','mail_encryption_id','frontend_base_url','mail_frontend_url'
        )
    ) OR pg_catalog.jsonb_typeof(v_derived->'password_hash') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'claims') NOT IN ('object','null')
      OR pg_catalog.jsonb_typeof(v_derived->'token') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'batch_tokens') NOT IN ('array','null')
      OR pg_catalog.jsonb_typeof(v_derived->'mail_token_hash') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'mail_token_ciphertext') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'mail_encryption_id') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'frontend_base_url') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'mail_frontend_url') NOT IN ('string','null') THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    IF (pg_catalog.jsonb_typeof(v_derived->'password_hash') = 'string'
        AND ((v_derived->>'password_hash') !~ '^\$2[aby]\$12\$[./A-Za-z0-9]{53}$'
             OR pg_catalog.char_length(v_derived->>'password_hash') <> 60))
       OR (pg_catalog.jsonb_typeof(v_derived->'token') = 'string'
           AND pg_catalog.char_length(v_derived->>'token') NOT BETWEEN 1 AND 64)
       OR (pg_catalog.jsonb_typeof(v_derived->'mail_token_hash') = 'string'
           AND (v_derived->>'mail_token_hash') COLLATE "C" !~ '^[0-9a-f]{64}$')
       OR (pg_catalog.jsonb_typeof(v_derived->'mail_encryption_id') = 'string'
           AND (v_derived->>'mail_encryption_id') COLLATE "C" !~ '^[0-9a-f]{64}$')
       OR (pg_catalog.jsonb_typeof(v_derived->'mail_token_ciphertext') = 'string'
           AND pg_catalog.char_length(v_derived->>'mail_token_ciphertext') NOT BETWEEN 1 AND 4096)
       OR (pg_catalog.jsonb_typeof(v_derived->'frontend_base_url') = 'string'
           AND pg_catalog.char_length(v_derived->>'frontend_base_url') < 1)
       OR (pg_catalog.jsonb_typeof(v_derived->'mail_frontend_url') = 'string'
           AND pg_catalog.char_length(v_derived->>'mail_frontend_url') < 1) THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    IF v_operation IN ('create_invitation','resend_invitation') THEN
        IF v_derived->'token' IS NULL OR v_derived->'token' = 'null'::jsonb
           OR v_derived->'frontend_base_url' IS NULL OR v_derived->'frontend_base_url' = 'null'::jsonb
           OR EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                      WHERE pair.key NOT IN ('token','frontend_base_url') AND pair.value <> 'null'::jsonb) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    ELSIF v_operation = 'register_invitation' THEN
        IF v_derived->'password_hash' = 'null'::jsonb OR v_derived->'claims' = 'null'::jsonb
           OR EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                      WHERE pair.key NOT IN ('password_hash','claims') AND pair.value <> 'null'::jsonb) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    ELSIF v_operation = 'send_invitation_email' THEN
        IF v_derived->'mail_token_hash' = 'null'::jsonb
           OR v_derived->'mail_token_ciphertext' = 'null'::jsonb
           OR v_derived->'mail_encryption_id' = 'null'::jsonb
           OR v_derived->'mail_frontend_url' = 'null'::jsonb
           OR EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                      WHERE pair.key NOT IN (
                          'mail_token_hash','mail_token_ciphertext','mail_encryption_id','mail_frontend_url'
                      ) AND pair.value <> 'null'::jsonb) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    ELSIF v_operation = 'apply_invitation_batch' THEN
        IF v_derived->'batch_tokens' = 'null'::jsonb
           OR v_derived->'frontend_base_url' = 'null'::jsonb
           OR EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                      WHERE pair.key NOT IN ('batch_tokens','frontend_base_url') AND pair.value <> 'null'::jsonb) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    ELSIF EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                  WHERE pair.value <> 'null'::jsonb) THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    v_allow_null := v_operation = 'register_invitation';
    v_actor_parse := aap_identity_invitations_v1._actor(
        v_actor_input,v_expected_key_hash,v_allow_null,false
    );
    IF v_actor_parse->'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_actor_parse; END IF;
    IF v_actor_input IS NOT NULL AND pg_catalog.jsonb_typeof(v_actor_input) <> 'null' THEN
        BEGIN v_actor_id := (v_actor_input->>'actor_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END;
    ELSIF NOT v_allow_null THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;

    IF v_operation = 'create_invitation' THEN
        IF pg_catalog.jsonb_typeof(v_args->'project_id') = 'string' THEN
            v_project_id := (v_args->>'project_id')::uuid;
            v_project := aap_identity_invitations_v1._project_record(v_project_id);
            IF v_project IS NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_not_found','目标项目不存在'
                );
            END IF;
            v_owner_id := (v_project->>'owner_id')::uuid;
        END IF;
        SELECT account.id INTO v_target_id FROM public.users AS account
        WHERE account.email = v_args->>'email' AND account.is_active
        ORDER BY account.id LIMIT 1;
        v_snapshot_target_id := v_target_id;
        v_user_ids := ARRAY[v_actor_id,v_owner_id,v_target_id];
    ELSIF v_operation = 'apply_invitation_batch' THEN
        v_batch_snapshot := aap_identity_invitations_v1._batch_snapshot(
            v_actor_id,v_args->'items'
        );
        SELECT COALESCE(
            pg_catalog.array_agg((item.value #>> '{}')::uuid), ARRAY[]::uuid[]
        ) INTO v_user_ids
        FROM pg_catalog.jsonb_array_elements(v_batch_snapshot->'user_ids') AS item(value);
    ELSIF v_operation IN ('revoke_invitation','resend_invitation','send_invitation_email') THEN
        SELECT invitation.* INTO v_initial FROM public.user_invitations AS invitation
        WHERE invitation.id = (v_args->>'invitation_id')::uuid;
        IF NOT FOUND THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        IF v_operation = 'send_invitation_email' AND v_initial.project_id IS NOT NULL THEN
            v_project_id := v_initial.project_id;
            v_project := aap_identity_invitations_v1._project_record(v_project_id);
            IF v_project IS NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_not_found','目标项目已删除，无法接受邀请'
                );
            END IF;
            v_owner_id := (v_project->>'owner_id')::uuid;
        END IF;
        v_user_ids := ARRAY[v_actor_id,v_owner_id,
            CASE WHEN v_operation = 'send_invitation_email' THEN v_initial.invited_by END];
    ELSIF v_operation IN ('register_invitation','accept_invitation') THEN
        v_token := v_args->>'invitation_token';
        SELECT invitation.* INTO v_initial FROM public.user_invitations AS invitation
        WHERE invitation.token = v_token;
        IF NOT FOUND THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请链接无效'
            );
        END IF;
        v_project_id := v_initial.project_id;
        v_project := aap_identity_invitations_v1._project_record(v_project_id);
        IF v_project_id IS NOT NULL AND v_project IS NULL THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','project_not_found','目标项目已删除，无法接受邀请'
            );
        END IF;
        v_owner_id := (v_project->>'owner_id')::uuid;
        IF v_operation = 'register_invitation' THEN
            SELECT account.id INTO v_target_id FROM public.users AS account
            WHERE account.id = v_operation_id;
        ELSE
            v_target_id := v_actor_id;
        END IF;
        v_user_ids := ARRAY[v_initial.invited_by,v_owner_id,v_target_id,v_actor_id];
    ELSE
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    v_user_ids := ARRAY(SELECT DISTINCT id FROM unnest(v_user_ids) AS users(id)
                        WHERE id IS NOT NULL ORDER BY id);
    IF NOT aap_identity_invitations_v1._lock_users(v_user_ids) THEN
        RETURN aap_identity_invitations_v1._failure('current_scope_busy');
    END IF;
    v_actor_lock := aap_identity_invitations_v1._actor(
        v_actor_input,v_expected_key_hash,v_allow_null,true
    );
    IF v_actor_lock->'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_actor_lock; END IF;
    v_principal := v_actor_lock->'principal';
    v_actor_native := v_actor_lock->'actor';
    v_actor_role := v_actor_native->>'role';
    IF v_actor_id IS NOT NULL AND v_operation <> 'accept_invitation'
       AND v_actor_role NOT IN ('super_admin','project_admin') THEN
        RETURN aap_identity_invitations_v1._failure(
            'policy_rejected','manager_required','当前账号不再具备邀请权限'
        );
    END IF;
    v_policy := aap_identity_invitations_v1._policy(v_config);

    IF v_operation = 'apply_invitation_batch' THEN
        IF v_actor_role NOT IN ('super_admin','project_admin') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','manager_required','当前账号不再具备邀请权限'
            );
        END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-identity-invitation-operation:' || v_operation_id::text,0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        BEGIN
            PERFORM account.id FROM public.users AS account
            WHERE account.id = v_actor_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        SELECT * INTO v_receipt
        FROM public.identity_invitation_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_result := v_receipt.result;
            -- A batch receipt is not a durable permission grant. Recheck every
            -- successful Project scope and referenced invitation before
            -- returning the frozen response to a project administrator.
            FOR v_scope_project IN
                SELECT DISTINCT
                    (argument.value->>'project_id')::uuid AS project_id,
                    (v_batch_snapshot #>> ARRAY[
                        'owners', argument.value->>'index'
                    ])::uuid AS owner_id
                FROM pg_catalog.jsonb_array_elements(v_args->'items') AS argument(value)
                JOIN pg_catalog.jsonb_array_elements(v_result->'items') AS prior(value)
                  ON prior.value->>'index' = argument.value->>'index'
                WHERE prior.value->'ok' = 'true'::jsonb
                  AND pg_catalog.jsonb_typeof(argument.value->'project_id') = 'string'
                ORDER BY project_id
            LOOP
                IF v_scope_project.owner_id IS NULL THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                BEGIN
                    v_workspace := aap_workspace_v1.lock_invitation_project(
                        v_scope_project.project_id, v_scope_project.owner_id
                    );
                EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END;
                IF v_workspace->>'schema' IS DISTINCT FROM 'aap-workspace-invitation-project/v1'
                   OR v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                IF v_actor_role = 'project_admin'
                   AND v_scope_project.owner_id IS DISTINCT FROM v_actor_id THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
            END LOOP;
            FOR v_batch_item IN
                SELECT prior.value
                FROM pg_catalog.jsonb_array_elements(v_result->'items') AS prior(value)
                WHERE prior.value->'ok' = 'true'::jsonb
                ORDER BY (prior.value->>'invitation_id')::uuid
            LOOP
                BEGIN
                    SELECT invitation.* INTO v_invitation
                    FROM public.user_invitations AS invitation
                    WHERE invitation.id = (v_batch_item->>'invitation_id')::uuid
                      AND invitation.invited_by = v_actor_id
                      AND invitation.email = v_batch_item->>'email'
                      AND invitation.project_id IS NOT DISTINCT FROM
                          CASE WHEN pg_catalog.jsonb_typeof(v_batch_item->'project_id') = 'null'
                              THEN NULL ELSE (v_batch_item->>'project_id')::uuid END
                    FOR KEY SHARE NOWAIT;
                EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END;
                IF NOT FOUND THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
            END LOOP;
            v_replayed := true;
        ELSE
            v_result := aap_identity_invitations_v1._batch_effects(
                false,v_actor_id,v_actor_role,v_principal->>'email',v_context,
                v_policy,v_args->'items',v_derived->'batch_tokens',v_batch_snapshot
            );
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    v_operation_lock := pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-identity-invitation-operation:' || v_operation_id::text,0)
    );
    IF NOT v_operation_lock THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;

    IF v_operation = 'create_invitation' THEN
        IF v_args->>'role' NOT IN ('super_admin','project_admin','employee','viewer') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invalid_platform_role','非法角色: ' || (v_args->>'role')
            );
        END IF;
        IF v_project_id IS NULL AND v_args->'project_member_role' <> 'null'::jsonb THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','account_project_role','账号级邀请不得包含项目职责'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_args->>'project_member_role' NOT IN ('annotator','reviewer','viewer') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invalid_project_role',
                '目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_args->>'role' = 'viewer'
           AND v_args->>'project_member_role' <> 'viewer' THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','platform_project_role_incompatible',
                '账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_args->>'role' IN ('super_admin','project_admin') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','account_project_role','管理员账号不能通过项目邀请加入项目'
            );
        END IF;
        IF v_actor_role = 'project_admin' AND v_args->>'role' NOT IN ('employee','viewer') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','project_admin_role_forbidden','项目管理员仅能邀请员工或观察者'
            );
        END IF;
        BEGIN
            PERFORM account.id FROM public.users AS account
            WHERE account.id = v_actor_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(pg_catalog.hashtextextended(v_args->>'email',0)) THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END IF;
        IF v_project_id IS NOT NULL THEN
            v_workspace := aap_workspace_v1.lock_invitation_project(v_project_id,v_owner_id);
            IF v_workspace #>> '{error,code}' = 'project_missing' THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_not_found','目标项目不存在'
                );
            ELSIF v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN aap_identity_invitations_v1._failure('current_scope_busy');
            END IF;
            SELECT account.id,account.role,account.is_active INTO v_owner
            FROM public.users AS account WHERE account.id = v_owner_id;
            IF NOT FOUND OR NOT v_owner.is_active
               OR v_owner.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目当前没有可用的负责人'
                );
            END IF;
            IF v_actor_role = 'project_admin' AND v_owner_id <> v_actor_id THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_admin_scope','邀请人已不再管理目标项目'
                );
            END IF;
        END IF;
        SELECT * INTO v_receipt FROM public.identity_invitation_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.operation <> v_operation OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_invitation_id := (v_receipt.result #>> '{invitation,id}')::uuid;
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_invitation_id AND invitation.invited_by = v_actor_id
              AND invitation.email = v_args->>'email'
            FOR KEY SHARE NOWAIT;
            IF NOT FOUND OR v_invitation.project_id IS DISTINCT FROM v_project_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_result := v_receipt.result;
            v_replayed := true;
        ELSE
            IF aap_identity_invitations_v1.invitation_quota_reached(
                v_actor_id,v_now,(v_policy->>'max_invitations_per_day')::integer
            ) THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','daily_quota',
                    '每位管理员滚动 24 小时最多邀请 ' ||
                    (v_policy->>'max_invitations_per_day') || ' 人，请稍后再试'
                );
            END IF;
            SELECT account.id,account.role,account.is_active INTO v_target
            FROM public.users AS account
            WHERE account.email = v_args->>'email' AND account.is_active
            ORDER BY account.id LIMIT 1;
            IF FOUND THEN
                IF v_target.id IS DISTINCT FROM v_snapshot_target_id THEN
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END IF;
                v_target_id := v_target.id;
                IF v_project_id IS NULL THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','active_email_exists',
                        '邮箱 ' || (v_args->>'email') || ' 已注册，请选择目标项目后邀请该用户加入'
                    );
                END IF;
                IF v_target.role NOT IN ('employee','viewer')
                   OR (v_target.role = 'viewer' AND v_args->>'project_member_role' <> 'viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','incompatible_account_role',
                        aap_identity_invitations_v1._incompatible_role_detail(
                            v_target.role,v_args->>'project_member_role'
                        )
                    );
                END IF;
                IF aap_workspace_v1.read_invitation_membership(v_project_id,v_target_id) THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','already_member','该账号已是目标项目成员'
                    );
                END IF;
            ELSIF v_snapshot_target_id IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure('current_scope_busy');
            END IF;
            BEGIN
                FOR v_invitation IN
                    SELECT invitation.id FROM public.user_invitations AS invitation
                    WHERE invitation.email = v_args->>'email'
                      AND invitation.accepted_at IS NULL
                      AND (v_actor_role = 'super_admin' OR invitation.invited_by = v_actor_id)
                    ORDER BY invitation.id
                    FOR UPDATE NOWAIT
                LOOP
                    UPDATE public.user_invitations AS invitation
                    SET expires_at = v_now
                    WHERE invitation.id = v_invitation.id;
                END LOOP;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_identity_invitations_v1._failure('current_scope_busy');
            END;
            v_token := v_derived->>'token';
            INSERT INTO public.user_invitations (
                id,email,role,project_role,group_name,project_id,token,expires_at,invited_by
            ) VALUES (
                pg_catalog.gen_random_uuid(),v_args->>'email',v_args->>'role',
                v_args->>'project_member_role',v_args->>'group_name',v_project_id,
                v_token,v_now + pg_catalog.make_interval(days => (v_policy->>'invitation_ttl_days')::integer),
                v_actor_id
            ) RETURNING id INTO v_invitation_id;
            v_audit_id := aap_audit_v1.record_identity_invitation_event(
                'user.invite',
                pg_catalog.jsonb_build_object('actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role),
                pg_catalog.jsonb_build_object('type','user','id',v_args->>'email'),
                v_context,
                pg_catalog.jsonb_build_object(
                    'role',v_args->>'role','group_name',v_args->'group_name','project_id',v_args->'project_id'
                )
            );
            v_result := aap_identity_invitations_v1._empty_result()
                || pg_catalog.jsonb_build_object(
                    'invitation',aap_identity_invitations_v1._invitation_record(
                        v_invitation_id,CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END
                    ),'frontend_base_url',v_policy->'frontend_base_url'
                );
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result,audit_id
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result,v_audit_id
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    IF v_operation IN ('revoke_invitation','resend_invitation') THEN
        IF v_actor_role = 'project_admin'
           AND v_initial.invited_by IS DISTINCT FROM v_actor_id THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        BEGIN
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_initial.id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        IF NOT FOUND THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        SELECT * INTO v_receipt
        FROM public.identity_invitation_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_result := v_receipt.result;
            v_replayed := true;
        ELSE
            IF v_invitation.accepted_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_accepted','该邀请已被接受，无法操作'
                );
            END IF;
            IF v_operation = 'resend_invitation'
               AND v_actor_role = 'project_admin'
               AND v_invitation.role NOT IN ('employee','viewer') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','privileged_resend','项目管理员不能重发高权限邀请'
                );
            END IF;
            IF v_operation = 'revoke_invitation' THEN
                IF v_invitation.revoked_at IS NULL THEN
                    UPDATE public.user_invitations AS invitation
                    SET revoked_at = v_now, expires_at = v_now
                    WHERE invitation.id = v_invitation.id;
                END IF;
                v_detail := pg_catalog.jsonb_build_object(
                    'email',v_invitation.email,'role',v_invitation.role
                );
                v_audit_id := aap_audit_v1.record_identity_invitation_event(
                    'user.invite_revoke',
                    pg_catalog.jsonb_build_object(
                        'actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role
                    ),
                    pg_catalog.jsonb_build_object('type','invitation','id',v_invitation.id::text),
                    v_context,v_detail
                );
                v_result := aap_identity_invitations_v1._empty_result()
                    || pg_catalog.jsonb_build_object(
                        'invitation',aap_identity_invitations_v1._invitation_record(v_invitation.id,NULL)
                    );
            ELSE
                IF v_invitation.role IN ('annotator','reviewer') THEN
                    IF v_invitation.project_id IS NOT NULL AND v_invitation.project_role IS NULL THEN
                        v_invitation.project_role := v_invitation.role;
                    END IF;
                    v_invitation.role := 'employee';
                END IF;
                v_token := v_derived->>'token';
                UPDATE public.user_invitations AS invitation
                SET role = v_invitation.role,
                    project_role = v_invitation.project_role,
                    token = v_token,
                    expires_at = v_now + pg_catalog.make_interval(
                        days => (v_policy->>'invitation_ttl_days')::integer
                    ),
                    revoked_at = NULL
                WHERE invitation.id = v_invitation.id;
                v_invitation.role := v_invitation.role;
                v_invitation.token := v_token;
                v_invitation.expires_at := v_now + pg_catalog.make_interval(
                    days => (v_policy->>'invitation_ttl_days')::integer
                );
                v_invitation.revoked_at := NULL;
                v_detail := pg_catalog.jsonb_build_object(
                    'email',v_invitation.email,'role',v_invitation.role
                );
                v_audit_id := aap_audit_v1.record_identity_invitation_event(
                    'user.invite_resend',
                    pg_catalog.jsonb_build_object(
                        'actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role
                    ),
                    pg_catalog.jsonb_build_object('type','invitation','id',v_invitation.id::text),
                    v_context,v_detail
                );
                v_result := aap_identity_invitations_v1._empty_result()
                    || pg_catalog.jsonb_build_object(
                        'invitation',aap_identity_invitations_v1._invitation_record(v_invitation.id,NULL),
                        'frontend_base_url',v_derived->'frontend_base_url'
                    );
            END IF;
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result,audit_id
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result,v_audit_id
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    IF v_operation = 'send_invitation_email' THEN
        IF v_actor_role = 'project_admin'
           AND v_initial.invited_by IS DISTINCT FROM v_actor_id THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(v_initial.email,0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        IF v_project_id IS NOT NULL THEN
            v_workspace := aap_workspace_v1.lock_invitation_project(v_project_id,v_owner_id);
            IF v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_not_found','目标项目已删除，无法接受邀请'
                );
            END IF;
            v_project := pg_catalog.jsonb_build_object(
                'id',v_project_id,'name',v_workspace #>> '{data,name}',
                'owner_id',v_owner_id
            );
            SELECT account.id,account.role,account.is_active INTO v_owner
            FROM public.users AS account WHERE account.id = v_owner_id;
            IF NOT FOUND OR NOT v_owner.is_active
               OR v_owner.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目当前没有可用的负责人'
                );
            END IF;
        END IF;
        BEGIN
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_initial.id
            FOR KEY SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        IF NOT FOUND OR v_invitation.invited_by IS DISTINCT FROM v_initial.invited_by THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        SELECT * INTO v_receipt
        FROM public.identity_invitation_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            SELECT outbox.* INTO v_outbox
            FROM public.identity_credential_mail_outbox AS outbox
            WHERE outbox.id = v_operation_id AND outbox.purpose = 'invitation'
              AND outbox.invitation_id = v_invitation.id;
            IF NOT FOUND THEN RETURN aap_identity_invitations_v1._failure('operation_conflict'); END IF;
            v_result := v_receipt.result || pg_catalog.jsonb_build_object(
                'mail_status',CASE WHEN v_outbox.status = 'delivered' THEN 'sent'
                    WHEN v_outbox.status IN ('rejected','cancelled','expired') THEN 'failed'
                    ELSE 'pending' END,
                'mail_failure_code',CASE WHEN v_outbox.status = 'delivered' THEN NULL
                    ELSE v_outbox.last_reason END
            );
            v_replayed := true;
        ELSE
            IF v_invitation.accepted_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_used','该邀请已被使用'
                );
            ELSIF v_invitation.revoked_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_revoked','该邀请已撤销'
                );
            ELSIF v_invitation.expires_at <= v_now THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_expired','该邀请已过期'
                );
            END IF;
            SELECT account.id,account.role,account.is_active INTO v_inviter
            FROM public.users AS account WHERE account.id = v_invitation.invited_by;
            IF NOT FOUND OR NOT v_inviter.is_active
               OR v_inviter.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','issuer_inactive','邀请人已停用或不再具备管理权限'
                );
            END IF;
            IF v_invitation.role IN ('super_admin','project_admin') AND v_inviter.role <> 'super_admin' THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','issuer_role_invalid','该邀请权限已失效'
                );
            END IF;
            IF v_project_id IS NOT NULL AND v_inviter.role = 'project_admin'
               AND v_owner_id <> v_invitation.invited_by THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_admin_scope','邀请人已不再管理目标项目'
                );
            END IF;
            v_mail_hash := pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(v_invitation.token,'UTF8')),'hex'
            );
            IF v_derived->>'mail_token_hash' IS DISTINCT FROM v_mail_hash THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_project_role := CASE WHEN v_project_id IS NULL THEN NULL
                ELSE COALESCE(v_invitation.project_role,
                    CASE WHEN v_invitation.role IN ('annotator','reviewer','viewer')
                         THEN v_invitation.role END) END;
            IF v_project_id IS NOT NULL AND v_project_role NOT IN ('annotator','reviewer','viewer') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invalid_project_role',
                    '目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色'
                );
            END IF;
            v_mail_context := pg_catalog.jsonb_build_object(
                'project_name',CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END,
                'project_member_role',v_project_role,
                'sender_actor',pg_catalog.jsonb_build_object(
                    'actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role
                ),
                'audit_context',v_context
            );
            INSERT INTO public.identity_credential_mail_outbox (
                id,actor_id,challenge_id,purpose,email,frontend_base_url,
                token_hash,token_ciphertext,encryption_id,expires_at,
                invitation_id,invitation_context
            ) VALUES (
                v_operation_id,v_invitation.invited_by,NULL,'invitation',v_invitation.email,
                v_derived->>'mail_frontend_url',v_mail_hash,
                v_derived->>'mail_token_ciphertext',v_derived->>'mail_encryption_id',
                v_invitation.expires_at,v_invitation.id,v_mail_context
            );
            v_result := aap_identity_invitations_v1._empty_result()
                || pg_catalog.jsonb_build_object(
                    'invitation',aap_identity_invitations_v1._invitation_record(
                        v_invitation.id,CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END
                    ),
                    'invitation_id',v_invitation.id,'email',v_invitation.email,
                    'mail_status','pending','mail_failure_code',NULL
                );
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    IF v_operation IN ('register_invitation','accept_invitation') THEN
        v_token := v_args->>'invitation_token';
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-identity-invitation-operation:' || v_operation_id::text,0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(v_initial.email,0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        IF v_project_id IS NOT NULL THEN
            v_workspace := aap_workspace_v1.lock_invitation_project(v_project_id,v_owner_id);
            IF v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_not_found','目标项目已删除，无法接受邀请'
                );
            END IF;
            v_project := pg_catalog.jsonb_build_object(
                'id',v_project_id,'name',v_workspace #>> '{data,name}',
                'owner_id',v_owner_id
            );
            SELECT account.id,account.name,account.role,account.is_active INTO v_owner
            FROM public.users AS account WHERE account.id = v_owner_id;
            IF NOT FOUND OR NOT v_owner.is_active
               OR v_owner.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目当前没有可用的负责人'
                );
            END IF;
        END IF;
        BEGIN
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_initial.id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        IF NOT FOUND OR v_invitation.token IS DISTINCT FROM v_token THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请链接无效'
            );
        END IF;
        SELECT account.id,account.name,account.email,account.role,account.is_active
        INTO v_inviter FROM public.users AS account WHERE account.id = v_invitation.invited_by;
        IF NOT FOUND OR NOT v_inviter.is_active
           OR v_inviter.role NOT IN ('super_admin','project_admin') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','issuer_inactive','邀请人已停用或不再具备管理权限'
            );
        END IF;
        IF v_invitation.role IN ('super_admin','project_admin') AND v_inviter.role <> 'super_admin' THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','issuer_role_invalid','该邀请权限已失效'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_inviter.role = 'project_admin'
           AND v_owner_id <> v_invitation.invited_by THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','project_admin_scope','邀请人已不再管理目标项目'
            );
        END IF;
        IF v_invitation.accepted_at IS NOT NULL THEN
            IF v_operation = 'register_invitation'
               AND v_invitation.accepted_user_id = v_operation_id THEN
                SELECT * INTO v_receipt
                FROM public.identity_invitation_operation_receipts AS receipt
                WHERE receipt.operation_id = v_operation_id;
                IF NOT FOUND OR v_receipt.operation <> v_operation
                   OR v_receipt.actor_id IS NOT NULL
                   OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
                   OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                SELECT account.* INTO v_target FROM public.users AS account
                WHERE account.id = v_operation_id AND account.is_active
                  AND account.email = v_invitation.email;
                IF NOT FOUND THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','actor_inactive','账号已停用，请联系管理员'
                    );
                END IF;
                IF v_target.password_hash IS DISTINCT FROM v_derived->>'password_hash'
                   OR v_target.credential_generation IS DISTINCT FROM
                      (v_receipt.result #>> '{claims,gen}')::bigint
                   OR v_target.role IS DISTINCT FROM v_receipt.result #>> '{claims,role}'
                   OR v_receipt.result->'claims' IS DISTINCT FROM v_derived->'claims' THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                v_result := v_receipt.result;
                v_replayed := true;
            ELSIF v_operation = 'accept_invitation' THEN
                SELECT * INTO v_receipt
                FROM public.identity_invitation_operation_receipts AS receipt
                WHERE receipt.operation_id = v_operation_id;
                IF NOT FOUND OR v_receipt.operation <> v_operation
                   OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
                   OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
                   OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id
                   OR v_invitation.accepted_user_id IS DISTINCT FROM v_actor_id THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                v_result := v_receipt.result;
                v_replayed := true;
            ELSE
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_used','该邀请已被使用'
                );
            END IF;
        ELSE
            SELECT * INTO v_receipt
            FROM public.identity_invitation_operation_receipts AS receipt
            WHERE receipt.operation_id = v_operation_id;
            IF FOUND THEN RETURN aap_identity_invitations_v1._failure('operation_conflict'); END IF;
            IF v_invitation.revoked_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_revoked','该邀请已撤销'
                );
            END IF;
            IF v_invitation.expires_at <= v_now THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_expired','该邀请已过期'
                );
            END IF;
            v_project_role := CASE WHEN v_project_id IS NULL THEN NULL
                ELSE COALESCE(v_invitation.project_role,
                    CASE WHEN v_invitation.role IN ('annotator','reviewer','viewer')
                         THEN v_invitation.role END) END;
            IF v_operation = 'register_invitation' THEN
                IF v_invitation.role NOT IN ('super_admin','project_admin','employee','viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','legacy_platform_role','该邀请包含已失效的账号角色，请联系管理员重发'
                    );
                END IF;
                IF v_project_id IS NOT NULL AND v_invitation.role IN ('super_admin','project_admin') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','account_project_role','管理员账号不能通过项目邀请加入项目'
                    );
                END IF;
                IF pg_catalog.char_length(COALESCE(v_invitation.group_name,'')) > 100 THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','group_name_invalid','邀请中的数据组名称无效，请联系管理员重发'
                    );
                END IF;
                IF EXISTS (SELECT 1 FROM public.users AS account
                           WHERE account.email = v_invitation.email) THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','email_exists','该邮箱已注册，请让该用户登录后确认项目邀请'
                    );
                END IF;
                v_password_hash := v_derived->>'password_hash';
                v_claims := v_derived->'claims';
                SELECT pg_catalog.count(*)::integer INTO v_count
                FROM pg_catalog.jsonb_object_keys(v_claims);
                IF v_count <> 6 OR NOT (v_claims ?& ARRAY['sub','role','exp','jti','gen','iss'])
                   OR EXISTS (
                       SELECT 1 FROM pg_catalog.jsonb_object_keys(v_claims) AS field(name)
                       WHERE field.name NOT IN ('sub','role','exp','jti','gen','iss')
                   ) OR pg_catalog.jsonb_typeof(v_claims->'sub') IS DISTINCT FROM 'string'
                   OR v_claims->>'sub' IS DISTINCT FROM v_operation_id::text
                   OR pg_catalog.jsonb_typeof(v_claims->'jti') IS DISTINCT FROM 'string'
                   OR v_claims->>'jti' IS DISTINCT FROM v_operation_id::text
                   OR pg_catalog.jsonb_typeof(v_claims->'role') IS DISTINCT FROM 'string'
                   OR v_claims->>'role' IS DISTINCT FROM v_invitation.role
                   OR pg_catalog.jsonb_typeof(v_claims->'exp') IS DISTINCT FROM 'number'
                   OR (v_claims->>'exp') !~ '^[1-9][0-9]*$'
                   OR (v_claims->>'exp')::numeric > 9223372036854775807
                   OR pg_catalog.jsonb_typeof(v_claims->'gen') IS DISTINCT FROM 'number'
                   OR v_claims->>'gen' IS DISTINCT FROM '0'
                   OR pg_catalog.jsonb_typeof(v_claims->'iss') IS DISTINCT FROM 'string'
                   OR v_claims->>'iss' IS DISTINCT FROM 'aap-identity/v1' THEN
                    RETURN aap_identity_invitations_v1._failure('invalid_request');
                END IF;
                IF pg_catalog.to_timestamp((v_claims->>'exp')::double precision) <= v_now THEN
                    RETURN aap_identity_invitations_v1._failure('token_expired');
                END IF;
                v_group_name := aap_identity_invitations_v1._strip_unicode_space(v_invitation.group_name);
                IF v_group_name = '' THEN v_group_name := NULL; END IF;
                IF v_group_name IS NOT NULL THEN
                    v_group := aap_identity_groups_v1.resolve_invitation_group(
                        v_invitation.id,v_invitation.token
                    );
                    IF pg_catalog.jsonb_typeof(v_group->'error') = 'object' THEN
                        IF v_group #>> '{error,code}' = 'invalid_group_name' THEN
                            RETURN aap_identity_invitations_v1._failure(
                                'policy_rejected','group_name_invalid','邀请中的数据组名称无效，请联系管理员重发'
                            );
                        END IF;
                        RETURN aap_identity_invitations_v1._failure(
                            'policy_rejected','invitation_not_found','邀请链接无效'
                        );
                    END IF;
                    v_group_id := (v_group->>'id')::uuid;
                    v_group_name := v_group->>'name';
                    UPDATE public.user_invitations AS invitation
                    SET group_name = v_group_name WHERE invitation.id = v_invitation.id;
                END IF;
                INSERT INTO public.users (
                    id,email,name,password_hash,credential_generation,role,group_name,group_id,
                    status,is_active,email_verified_at
                ) VALUES (
                    v_operation_id,v_invitation.email,v_args->>'name',v_password_hash,0,
                    v_invitation.role,v_group_name,v_group_id,'online',true,v_now
                );
                v_user_id := v_operation_id;
                IF v_project_id IS NOT NULL THEN
                    v_member_reply := aap_workspace_v1.grant_invitation_membership(
                        v_invitation.invited_by,v_project_id,v_project_role,
                        v_user_id,v_owner_id
                    );
                    IF v_member_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'invitation membership scope changed';
                    END IF;
                END IF;
                UPDATE public.user_invitations AS invitation
                SET accepted_at = v_now, accepted_user_id = v_user_id
                WHERE invitation.id = v_invitation.id;
                v_acceptance := pg_catalog.jsonb_build_object(
                    'project_id',v_project_id,
                    'project_name',CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END,
                    'project_member_role',v_project_role,
                    'next_action','wait_for_allocation',
                    'next_action_label',CASE WHEN v_project_id IS NULL THEN '等待管理员分配项目'
                                             ELSE '等待分派' END,
                    'responsible_person_name',CASE WHEN v_project_id IS NULL THEN NULL ELSE v_owner.name END,
                    'active_batch_count',0
                );
                IF v_project_id IS NOT NULL AND v_project_role IN ('annotator','reviewer') THEN
                    v_active_count := aap_labeling_v1.invitation_work_summary(
                        v_project_id,v_user_id,v_project_role
                    );
                    v_acceptance := v_acceptance || pg_catalog.jsonb_build_object(
                        'active_batch_count',v_active_count,
                        'next_action',CASE WHEN v_active_count > 0 THEN 'start_work' ELSE 'wait_for_allocation' END,
                        'next_action_label',CASE WHEN v_active_count > 0 THEN '开始工作' ELSE '等待分派' END
                    );
                END IF;
                v_audit_id := aap_audit_v1.record_identity_invitation_event(
                    'user.register',
                    pg_catalog.jsonb_build_object(
                        'actor_id',v_user_id,'email',v_invitation.email,'role',v_invitation.role
                    ),
                    pg_catalog.jsonb_build_object('type','user','id',v_user_id::text),
                    v_context,
                    pg_catalog.jsonb_build_object(
                        'email',v_invitation.email,'role',v_invitation.role,
                        'invitation_id',v_invitation.id,'project_id',v_project_id
                    )
                );
                v_result := aap_identity_invitations_v1._empty_result()
                    || pg_catalog.jsonb_build_object(
                        'account',aap_identity_invitations_v1._account_record(v_user_id),
                        'claims',v_claims,'acceptance',v_acceptance,
                        'email_verification_required',false
                    );
            ELSE
                IF v_project_id IS NULL THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','account_only_invitation',
                        '该邀请仅用于创建新账号，已有账号只能接受带目标项目的邀请'
                    );
                END IF;
                IF v_project_role NOT IN ('annotator','reviewer','viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','invalid_project_role',
                        '目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色'
                    );
                END IF;
                IF v_actor_native->>'email' IS DISTINCT FROM v_invitation.email THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','email_mismatch','当前登录邮箱与邀请邮箱不一致，请使用被邀请邮箱登录'
                    );
                END IF;
                IF v_actor_role NOT IN ('employee','viewer')
                   OR (v_actor_role = 'viewer' AND v_project_role <> 'viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','incompatible_account_role',
                        aap_identity_invitations_v1._incompatible_role_detail(
                            v_actor_role,v_project_role
                        )
                    );
                END IF;
                IF aap_workspace_v1.read_invitation_membership(v_project_id,v_actor_id) THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','already_member','该账号已是目标项目成员'
                    );
                END IF;
                v_member_reply := aap_workspace_v1.grant_invitation_membership(
                    v_invitation.invited_by,v_project_id,v_project_role,v_actor_id,v_owner_id
                );
                IF v_member_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    IF v_member_reply #>> '{error,code}' = 'membership_exists' THEN
                        RETURN aap_identity_invitations_v1._failure(
                            'policy_rejected','already_member','该账号已是目标项目成员'
                        );
                    END IF;
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END IF;
                UPDATE public.user_invitations AS invitation
                SET accepted_at = v_now, accepted_user_id = v_actor_id
                WHERE invitation.id = v_invitation.id;
                v_active_count := 0;
                IF v_project_role IN ('annotator','reviewer') THEN
                    v_active_count := aap_labeling_v1.invitation_work_summary(
                        v_project_id,v_actor_id,v_project_role
                    );
                END IF;
                v_acceptance := pg_catalog.jsonb_build_object(
                    'project_id',v_project_id,'project_name',v_project->>'name',
                    'project_member_role',v_project_role,
                    'next_action',CASE WHEN v_active_count > 0 THEN 'start_work' ELSE 'wait_for_allocation' END,
                    'next_action_label',CASE WHEN v_active_count > 0 THEN '开始工作' ELSE '等待分派' END,
                    'responsible_person_name',v_owner.name,
                    'active_batch_count',v_active_count
                );
                v_audit_id := aap_audit_v1.record_identity_invitation_event(
                    'user.invite_accept_existing',
                    pg_catalog.jsonb_build_object(
                        'actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role
                    ),
                    pg_catalog.jsonb_build_object('type','invitation','id',v_invitation.id::text),
                    v_context,
                    pg_catalog.jsonb_build_object('email',v_actor_native->>'email','project_id',v_project_id)
                );
                v_result := aap_identity_invitations_v1._empty_result()
                    || pg_catalog.jsonb_build_object(
                        'account',aap_identity_invitations_v1._account_record(v_actor_id),
                        'acceptance',v_acceptance
                    );
            END IF;
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result,audit_id
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result,v_audit_id
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    -- Other transactional operations are handled below.
    RETURN aap_identity_invitations_v1._failure('unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1.apply_invitation_command(jsonb)
    FROM PUBLIC;
