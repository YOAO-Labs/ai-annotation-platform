-- Native transactional ownership for the Identity group catalog and assignment.
CREATE SCHEMA aap_identity_groups_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_groups_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_group_operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    operation text NOT NULL CHECK (operation IN (
        'create_group', 'update_group', 'delete_group',
        'assign_user_group', 'apply_group_assignments'
    )),
    request_fingerprint text NOT NULL CHECK (
        request_fingerprint COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    derivation_id text NOT NULL CHECK (derivation_id COLLATE "C" ~ '^[0-9a-f]{64}$'),
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE INDEX ix_identity_group_operation_receipts_actor
    ON public.identity_group_operation_receipts (actor_id, created_at DESC);
-- aap-statement-break
COMMENT ON TABLE public.identity_group_operation_receipts IS
    'HMAC-scoped Identity group mutation receipts; no bearer or request metadata is retained.';
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_group_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_groups_v1._failure(
    p_code text,
    p_status_code smallint DEFAULT NULL,
    p_detail jsonb DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-groups-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object(
            'code', p_code, 'status_code', p_status_code, 'detail', p_detail
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_groups_v1._failure(text, smallint, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_groups_v1._strip_unicode_space(p_value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.btrim(
        p_value,
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
REVOKE ALL ON FUNCTION aap_identity_groups_v1._strip_unicode_space(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_groups_v1._account_record(p_user_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
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
REVOKE ALL ON FUNCTION aap_identity_groups_v1._account_record(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_groups_v1._group_record(p_group_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', group_row.id,
        'name', group_row.name,
        'description', group_row.description,
        'member_count', (
            SELECT pg_catalog.count(*)::integer
            FROM public.users AS account
            WHERE account.group_id = group_row.id AND account.is_active
        ),
        'created_at', group_row.created_at
    )
    FROM public.groups AS group_row
    WHERE group_row.id = p_group_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_groups_v1._group_record(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_groups_v1._group_actor(
    p_actor jsonb,
    p_expected_key_hash text
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
    v_expiry timestamptz;
    v_user_id uuid;
    v_name text;
    v_email text;
    v_role text;
    v_active boolean;
    v_current_generation bigint;
    v_key_hash text;
    v_scopes jsonb;
    v_key_expiry timestamptz;
BEGIN
    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_groups_v1._failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_actor);
    IF v_count <> 10 OR NOT (p_actor ?& ARRAY[
        'actor_id', 'name', 'email', 'role', 'is_active', 'credential_type',
        'credential_id', 'generation', 'scopes', 'expires_at'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_actor) AS field(name)
        WHERE field.name NOT IN (
            'actor_id', 'name', 'email', 'role', 'is_active', 'credential_type',
            'credential_id', 'generation', 'scopes', 'expires_at'
        )
    ) OR pg_catalog.jsonb_typeof(p_actor->'actor_id') IS DISTINCT FROM 'string'
      OR (p_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      OR pg_catalog.jsonb_typeof(p_actor->'name') IS DISTINCT FROM 'string'
      OR pg_catalog.char_length(p_actor->>'name') > 100
      OR pg_catalog.jsonb_typeof(p_actor->'email') IS DISTINCT FROM 'string'
      OR pg_catalog.char_length(p_actor->>'email') > 255
      OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
      OR p_actor->>'role' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer')
      OR p_actor->'is_active' IS DISTINCT FROM 'true'::jsonb
      OR pg_catalog.jsonb_typeof(p_actor->'credential_type') IS DISTINCT FROM 'string'
      OR p_actor->>'credential_type' NOT IN ('jwt', 'api_key')
      OR pg_catalog.jsonb_typeof(p_actor->'credential_id') IS DISTINCT FROM 'string'
      OR (p_actor->>'credential_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN aap_identity_groups_v1._failure('invalid_request');
    END IF;
    BEGIN
        v_actor_id := (p_actor->>'actor_id')::uuid;
        v_credential_id := (p_actor->>'credential_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_groups_v1._failure('invalid_request');
    END;
    v_kind := p_actor->>'credential_type';

    IF v_kind = 'jwt' THEN
        IF pg_catalog.jsonb_typeof(p_actor->'generation') IS DISTINCT FROM 'number'
           OR (p_actor->>'generation') !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.jsonb_typeof(p_actor->'scopes') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_actor->'expires_at') IS DISTINCT FROM 'string'
           OR (p_actor->>'expires_at') !~* (
               '(z|[+-][0-9]{2}' || pg_catalog.chr(58) || '[0-9]{2})$'
           ) OR p_expected_key_hash IS NOT NULL THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END IF;
        BEGIN
            v_generation := (p_actor->>'generation')::bigint;
            v_expiry := (p_actor->>'expires_at')::timestamptz;
        EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format
                     OR datetime_field_overflow OR numeric_value_out_of_range THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END;
        IF NOT pg_catalog.isfinite(v_expiry) THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END IF;
    ELSE
        IF pg_catalog.jsonb_typeof(p_actor->'generation') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_actor->'scopes') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_actor->'expires_at') NOT IN ('null', 'string')
           OR p_expected_key_hash IS NULL
           OR pg_catalog.octet_length(p_expected_key_hash) NOT BETWEEN 1 AND 255
           OR (pg_catalog.jsonb_typeof(p_actor->'expires_at') = 'string'
               AND (p_actor->>'expires_at') !~* (
                   '(z|[+-][0-9]{2}' || pg_catalog.chr(58) || '[0-9]{2})$'
               )) OR EXISTS (
                   SELECT 1 FROM pg_catalog.jsonb_array_elements(p_actor->'scopes') AS item(value)
                   WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
               ) THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END IF;
        IF pg_catalog.jsonb_typeof(p_actor->'expires_at') = 'string' THEN
            BEGIN
                v_expiry := (p_actor->>'expires_at')::timestamptz;
            EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format
                         OR datetime_field_overflow THEN
                RETURN aap_identity_groups_v1._failure('invalid_request');
            END;
            IF NOT pg_catalog.isfinite(v_expiry) THEN
                RETURN aap_identity_groups_v1._failure('invalid_request');
            END IF;
        END IF;
    END IF;

    BEGIN
        SELECT account.id, account.name, account.email, account.role,
               account.is_active, account.credential_generation
        INTO v_user_id, v_name, v_email, v_role, v_active, v_current_generation
        FROM public.users AS account
        WHERE account.id = v_actor_id
        FOR KEY SHARE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_groups_v1._failure('account_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_identity_groups_v1._failure('credential_invalid');
    END IF;
    IF NOT v_active THEN
        RETURN aap_identity_groups_v1._failure('user_inactive');
    END IF;
    IF v_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RETURN aap_identity_groups_v1._failure('invalid_account_role');
    END IF;

    IF v_kind = 'jwt' THEN
        IF v_current_generation IS DISTINCT FROM v_generation THEN
            RETURN aap_identity_groups_v1._failure('generation_outdated');
        END IF;
        IF v_expiry <= pg_catalog.clock_timestamp() THEN
            RETURN aap_identity_groups_v1._failure('token_expired');
        END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock_shared(
            pg_catalog.hashtextextended('aap-identity-jti:' || v_credential_id::text, 0)
        ) THEN
            RETURN aap_identity_groups_v1._failure('credential_busy');
        END IF;
        IF aap_identity_v1.is_jti_revoked(v_credential_id) THEN
            RETURN aap_identity_groups_v1._failure('credential_revoked');
        END IF;
    ELSE
        BEGIN
            SELECT key.key_hash, key.scopes, key.expires_at
            INTO v_key_hash, v_scopes, v_key_expiry
            FROM public.api_keys AS key
            WHERE key.id = v_credential_id AND key.user_id = v_actor_id
              AND key.revoked_at IS NULL
            FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_groups_v1._failure('credential_busy');
        END;
        IF NOT FOUND THEN
            RETURN aap_identity_groups_v1._failure('credential_revoked');
        END IF;
        IF v_key_hash IS DISTINCT FROM p_expected_key_hash
           OR v_scopes IS DISTINCT FROM p_actor->'scopes'
           OR v_key_expiry IS DISTINCT FROM v_expiry THEN
            RETURN aap_identity_groups_v1._failure('credential_invalid');
        END IF;
        IF v_key_expiry IS NOT NULL AND v_key_expiry <= pg_catalog.clock_timestamp() THEN
            RETURN aap_identity_groups_v1._failure('credential_revoked');
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', v_user_id, 'name', v_name, 'email', v_email, 'role', v_role,
            'is_active', true, 'credential_type', v_kind,
            'credential_id', v_credential_id,
            'generation', CASE WHEN v_kind = 'jwt' THEN v_current_generation END,
            'scopes', CASE WHEN v_kind = 'api_key' THEN v_scopes END,
            'expires_at', CASE WHEN v_kind = 'jwt' THEN v_expiry ELSE v_key_expiry END
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_groups_v1._group_actor(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.lock_group_assignment_scope(
    p_actor_id uuid,
    p_user_ids uuid[],
    p_write boolean
)
RETURNS uuid[]
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_users uuid[];
    v_project_id uuid;
    v_locked_projects uuid[] := ARRAY[]::uuid[];
BEGIN
    IF p_actor_id IS NULL OR p_user_ids IS NULL
       OR p_write IS NULL
       OR pg_catalog.cardinality(p_user_ids) > 500
       OR pg_catalog.array_position(p_user_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid group scope request';
    END IF;

    IF p_write THEN
        -- Membership writers take Project before ProjectMember. Capture and
        -- hold this exact owner set; later-created projects are excluded below.
        FOR v_project_id IN
            SELECT project.id FROM public.projects AS project
            WHERE project.owner_id = p_actor_id
            ORDER BY project.id
            FOR SHARE OF project NOWAIT
        LOOP
            v_locked_projects := pg_catalog.array_append(v_locked_projects, v_project_id);
        END LOOP;

        PERFORM member.project_id
        FROM public.project_members AS member
        JOIN public.projects AS project ON project.id = member.project_id
        WHERE project.id = ANY(v_locked_projects)
          AND member.user_id = ANY(p_user_ids)
        ORDER BY member.project_id, member.user_id
        FOR SHARE OF member NOWAIT;
    END IF;

    SELECT COALESCE(pg_catalog.array_agg(DISTINCT member.user_id ORDER BY member.user_id),
                    ARRAY[]::uuid[])
    INTO v_users
    FROM public.project_members AS member
    JOIN public.projects AS project ON project.id = member.project_id
    WHERE project.owner_id = p_actor_id
      AND (NOT p_write OR project.id = ANY(v_locked_projects))
      AND member.user_id = ANY(p_user_ids);
    RETURN v_users;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.lock_group_assignment_scope(
    uuid, uuid[], boolean
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_identity_group_event(
    p_event text,
    p_actor jsonb,
    p_context jsonb,
    p_envelope jsonb
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
    v_detail jsonb;
    v_action text;
    v_status smallint;
    v_audit_id bigint;
    v_id uuid;
BEGIN
    IF p_event IS NULL OR p_event NOT IN (
        'group.create', 'group.update', 'group.delete',
        'user.group_change', 'user.bulk_group_assign'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity group Audit event';
    END IF;
    IF pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object'
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity group Audit context';
    END IF;
    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity group Audit actor';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_actor);
    IF v_count <> 3 OR NOT (p_actor ?& ARRAY['actor_id', 'email', 'role'])
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
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity group Audit actor';
    END IF;
    BEGIN
        v_actor_id := (p_actor->>'actor_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity group Audit actor';
    END;
    v_actor_email := p_actor->>'email';
    v_actor_role := p_actor->>'role';

    IF pg_catalog.jsonb_typeof(p_envelope) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity group Audit target';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_envelope);
    IF v_count <> 3 OR NOT (p_envelope ?& ARRAY['target_type', 'target_id', 'detail'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_envelope) AS field(name)
           WHERE field.name NOT IN ('target_type', 'target_id', 'detail')
       )
       OR pg_catalog.jsonb_typeof(p_envelope->'target_type') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_envelope->'target_id') IS DISTINCT FROM 'string'
       OR (p_envelope->>'target_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_envelope->'detail') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity group Audit target';
    END IF;
    v_target_type := p_envelope->>'target_type';
    v_target_id := p_envelope->>'target_id';
    v_detail := p_envelope->'detail';
    BEGIN
        v_id := v_target_id::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity group Audit target';
    END;

    IF p_event LIKE 'group.%' THEN
        IF v_target_type <> 'group' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity group Audit target';
        END IF;
        IF p_event = 'group.create' THEN
            v_action := p_event; v_status := 201;
        ELSIF p_event = 'group.update' THEN
            v_action := p_event; v_status := 200;
        ELSE
            v_action := p_event; v_status := 204;
        END IF;
        IF NOT (v_detail ? 'name')
           OR pg_catalog.jsonb_typeof(v_detail->'name') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_detail->>'name') NOT BETWEEN 1 AND 220
           OR (p_event <> 'group.update' AND (
               (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(v_detail)) <> 1
           ))
           OR (p_event = 'group.update' AND (
               (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(v_detail)) NOT BETWEEN 1 AND 2
               OR EXISTS (
                   SELECT 1 FROM pg_catalog.jsonb_object_keys(v_detail) AS field(name)
                   WHERE field.name NOT IN ('name', 'description')
               )
               OR (v_detail ? 'description' AND (
                   pg_catalog.jsonb_typeof(v_detail->'description') IS DISTINCT FROM 'string'
                   OR v_detail->>'description' <> 'updated'
               ))
           )) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity group Audit detail';
        END IF;
    ELSE
        IF v_target_type <> 'user' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity group Audit target';
        END IF;
        v_action := p_event; v_status := 200;
        IF p_event = 'user.group_change' THEN
            IF (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(v_detail)) <> 5
               OR NOT (v_detail ?& ARRAY[
                   'email', 'old_group_id', 'old_group_name', 'new_group_id', 'new_group_name'
               ])
               OR EXISTS (
                   SELECT 1 FROM pg_catalog.jsonb_object_keys(v_detail) AS field(name)
                   WHERE field.name NOT IN (
                       'email', 'old_group_id', 'old_group_name', 'new_group_id', 'new_group_name'
                   )
               )
               OR pg_catalog.jsonb_typeof(v_detail->'email') IS DISTINCT FROM 'string'
               OR pg_catalog.char_length(v_detail->>'email') > 255
               OR pg_catalog.jsonb_typeof(v_detail->'old_group_id') NOT IN ('null', 'string')
               OR (pg_catalog.jsonb_typeof(v_detail->'old_group_id') = 'string'
                   AND (v_detail->>'old_group_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR pg_catalog.jsonb_typeof(v_detail->'new_group_id') NOT IN ('null', 'string')
               OR (pg_catalog.jsonb_typeof(v_detail->'new_group_id') = 'string'
                   AND (v_detail->>'new_group_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR pg_catalog.jsonb_typeof(v_detail->'old_group_name') NOT IN ('null', 'string')
               OR (pg_catalog.jsonb_typeof(v_detail->'old_group_name') = 'string'
                   AND pg_catalog.char_length(v_detail->>'old_group_name') > 100)
               OR pg_catalog.jsonb_typeof(v_detail->'new_group_name') NOT IN ('null', 'string')
               OR (pg_catalog.jsonb_typeof(v_detail->'new_group_name') = 'string'
                   AND pg_catalog.char_length(v_detail->>'new_group_name') > 100) THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity group Audit detail';
            END IF;
        ELSE
            IF (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(v_detail)) <> 2
               OR NOT (v_detail ?& ARRAY['group_id', 'group_name'])
               OR EXISTS (
                   SELECT 1 FROM pg_catalog.jsonb_object_keys(v_detail) AS field(name)
                   WHERE field.name NOT IN ('group_id', 'group_name')
               )
               OR pg_catalog.jsonb_typeof(v_detail->'group_id') NOT IN ('null', 'string')
               OR (pg_catalog.jsonb_typeof(v_detail->'group_id') = 'string'
                   AND (v_detail->>'group_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
               OR pg_catalog.jsonb_typeof(v_detail->'group_name') NOT IN ('null', 'string')
               OR (pg_catalog.jsonb_typeof(v_detail->'group_name') = 'string'
                   AND pg_catalog.char_length(v_detail->>'group_name') > 100) THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity group Audit detail';
            END IF;
        END IF;
    END IF;

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, detail_json, request_id
    ) VALUES (
        v_actor_id, v_actor_email, v_actor_role, v_action, v_target_type, v_target_id,
        p_context->>'method', p_context->>'path', v_status,
        NULLIF(p_context->>'ip', '')::inet, v_detail, p_context->>'request_id'
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_identity_group_event(
    text, jsonb, jsonb, jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_groups_v1._group_scope(
    p_actor_id uuid,
    p_user_ids uuid[],
    p_write boolean
)
RETURNS uuid[]
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_role text;
    v_needs_workspace uuid[];
    v_members uuid[];
    v_users uuid[];
BEGIN
    SELECT account.role INTO v_role
    FROM public.users AS account
    WHERE account.id = p_actor_id;
    IF NOT FOUND OR v_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN ARRAY[]::uuid[];
    END IF;
    IF v_role = 'super_admin' THEN
        RETURN p_user_ids;
    END IF;

    SELECT COALESCE(pg_catalog.array_agg(DISTINCT account.id ORDER BY account.id),
                    ARRAY[]::uuid[])
    INTO v_needs_workspace
    FROM public.users AS account
    WHERE account.id = ANY(p_user_ids)
      AND account.id <> p_actor_id
      AND NOT (account.is_active AND account.role = 'employee');
    IF pg_catalog.cardinality(v_needs_workspace) = 0 THEN
        v_members := ARRAY[]::uuid[];
    ELSE
        v_members := aap_workspace_v1.lock_group_assignment_scope(
            p_actor_id, v_needs_workspace, p_write
        );
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(DISTINCT account.id ORDER BY account.id),
                    ARRAY[]::uuid[])
    INTO v_users
    FROM public.users AS account
    WHERE account.id = ANY(p_user_ids)
      AND (
          account.id = p_actor_id
          OR (account.is_active AND account.role = 'employee')
          OR account.id = ANY(v_members)
      );
    RETURN v_users;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_groups_v1._group_scope(uuid, uuid[], boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_groups_v1.group_policy()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-groups-policy/v1',
        'policy', 'aap-identity-groups/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_groups_v1.group_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_groups_v1.execute_group_command(p_command jsonb)
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
    v_actor_id uuid;
    v_actor jsonb;
    v_actor_fence jsonb;
    v_principal jsonb;
    v_expected_key_hash text;
    v_fingerprint text;
    v_derivation_id text;
    v_context jsonb;
    v_payload jsonb;
    v_payload_count integer;
    v_group_id uuid;
    v_user_id uuid;
    v_old_name text;
    v_new_name text;
    v_description text;
    v_name_present boolean;
    v_description_present boolean;
    v_group_changed boolean := false;
    v_group_row public.groups%ROWTYPE;
    v_target record;
    v_actor_role text;
    v_scope uuid[];
    v_ids uuid[];
    v_items jsonb;
    v_item jsonb;
    v_item_error text;
    v_result jsonb;
    v_data jsonb;
    v_event_detail jsonb;
    v_has_receipt boolean := false;
    v_receipt public.identity_group_operation_receipts%ROWTYPE;
    v_page integer;
    v_page_size integer;
    v_search text;
    v_total bigint;
    v_succeeded integer := 0;
    v_failed integer := 0;
    v_in_scope boolean;
    v_is_write boolean;
    v_missing_group boolean := false;
    v_old_group_id uuid;
    v_old_group_name text;
    v_new_group_name text;
    v_audit_actor jsonb;
    v_allowed uuid[];
    v_index integer;
BEGIN
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_groups_v1._failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_command);
    IF v_count <> 9 OR NOT (p_command ?& ARRAY[
        'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
        'request_fingerprint', 'derivation_id', 'audit_context', 'payload'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_command) AS field(name)
        WHERE field.name NOT IN (
            'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
            'request_fingerprint', 'derivation_id', 'audit_context', 'payload'
        )
    ) OR pg_catalog.jsonb_typeof(p_command->'schema') IS DISTINCT FROM 'string'
      OR p_command->>'schema' IS DISTINCT FROM 'aap-identity-groups-native/v1'
      OR pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string'
      OR p_command->>'operation' NOT IN (
          'list_groups', 'query_groups', 'create_group', 'update_group',
          'delete_group', 'preview_group_assignments', 'apply_group_assignments',
          'assign_user_group'
      )
      OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
      OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      OR pg_catalog.jsonb_typeof(p_command->'expected_key_hash') NOT IN ('null', 'string')
      OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
      OR (p_command->>'request_fingerprint') !~ '^[0-9a-f]{64}$'
      OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
      OR (p_command->>'derivation_id') !~ '^[0-9a-f]{64}$'
      OR pg_catalog.jsonb_typeof(p_command->'audit_context') IS DISTINCT FROM 'object'
      OR pg_catalog.jsonb_typeof(p_command->'payload') IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_groups_v1._failure('invalid_request');
    END IF;
    BEGIN
        v_operation_id := (p_command->>'operation_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_groups_v1._failure('invalid_request');
    END;
    v_operation := p_command->>'operation';
    v_actor := p_command->'actor';
    v_expected_key_hash := CASE
        WHEN pg_catalog.jsonb_typeof(p_command->'expected_key_hash') = 'null' THEN NULL
        ELSE p_command->>'expected_key_hash'
    END;
    v_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';
    v_context := p_command->'audit_context';
    v_payload := p_command->'payload';
    IF NOT aap_audit_v1.valid_request_context(v_context) THEN
        RETURN aap_identity_groups_v1._failure('invalid_request');
    END IF;
    IF pg_catalog.jsonb_typeof(v_actor) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(v_actor->'actor_id') IS DISTINCT FROM 'string'
       OR (v_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN aap_identity_groups_v1._failure('invalid_request');
    END IF;
    BEGIN
        v_actor_id := (v_actor->>'actor_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_groups_v1._failure('invalid_request');
    END;
    v_is_write := v_operation IN (
        'create_group', 'update_group', 'delete_group',
        'assign_user_group', 'apply_group_assignments'
    );

    -- Validate the exact operation payload before acquiring any row locks.
    SELECT pg_catalog.count(*)::integer INTO v_payload_count
    FROM pg_catalog.jsonb_object_keys(v_payload);
    IF v_operation = 'list_groups' THEN
        IF v_payload_count <> 0 THEN RETURN aap_identity_groups_v1._failure('invalid_request'); END IF;
    ELSIF v_operation = 'query_groups' THEN
        IF v_payload_count <> 3 OR NOT (v_payload ?& ARRAY['page', 'page_size', 'search'])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_payload) AS field(name)
               WHERE field.name NOT IN ('page', 'page_size', 'search')
           ) OR pg_catalog.jsonb_typeof(v_payload->'page') IS DISTINCT FROM 'number'
           OR (v_payload->>'page') !~ '^[1-9][0-9]*$'
           OR pg_catalog.jsonb_typeof(v_payload->'page_size') IS DISTINCT FROM 'number'
           OR (v_payload->>'page_size') !~ '^[1-9][0-9]*$'
           OR pg_catalog.jsonb_typeof(v_payload->'search') NOT IN ('null', 'string') THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END IF;
        BEGIN
            v_page := (v_payload->>'page')::integer;
            v_page_size := (v_payload->>'page_size')::integer;
        EXCEPTION WHEN numeric_value_out_of_range THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END;
        IF v_page < 1 OR v_page_size NOT BETWEEN 1 AND 500 THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END IF;
        v_search := CASE
            WHEN pg_catalog.jsonb_typeof(v_payload->'search') = 'null' THEN NULL
            ELSE aap_identity_groups_v1._strip_unicode_space(v_payload->>'search')
        END;
    ELSIF v_operation = 'create_group' THEN
        IF v_payload_count NOT BETWEEN 2 AND 3 OR NOT (v_payload ? 'name')
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_payload) AS field(name)
               WHERE field.name NOT IN ('name', 'description')
           ) OR pg_catalog.jsonb_typeof(v_payload->'name') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_payload->>'name') NOT BETWEEN 1 AND 100
           OR (v_payload ? 'description' AND (
               pg_catalog.jsonb_typeof(v_payload->'description') NOT IN ('null', 'string')
               OR (pg_catalog.jsonb_typeof(v_payload->'description') = 'string'
                   AND pg_catalog.char_length(v_payload->>'description') > 500)
           )) THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END IF;
        v_new_name := aap_identity_groups_v1._strip_unicode_space(v_payload->>'name');
        v_description_present := v_payload ? 'description'
            AND pg_catalog.jsonb_typeof(v_payload->'description') = 'string';
        v_description := CASE WHEN v_description_present
            THEN NULLIF(v_payload->>'description', '') ELSE NULL END;
    ELSIF v_operation IN ('update_group', 'delete_group') THEN
        IF pg_catalog.jsonb_typeof(v_payload->'group_id') IS DISTINCT FROM 'string'
           OR (v_payload->>'group_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END IF;
        IF v_operation = 'delete_group' THEN
            IF v_payload_count <> 1 OR NOT (v_payload ? 'group_id') THEN
                RETURN aap_identity_groups_v1._failure('invalid_request');
            END IF;
        ELSE
            IF v_payload_count NOT BETWEEN 1 AND 3 OR EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_object_keys(v_payload) AS field(name)
                WHERE field.name NOT IN ('group_id', 'name', 'description')
            ) OR (v_payload ? 'name' AND (
                pg_catalog.jsonb_typeof(v_payload->'name') NOT IN ('null', 'string')
                OR (pg_catalog.jsonb_typeof(v_payload->'name') = 'string'
                    AND pg_catalog.char_length(v_payload->>'name') NOT BETWEEN 1 AND 100)
            )) OR (v_payload ? 'description' AND (
                pg_catalog.jsonb_typeof(v_payload->'description') NOT IN ('null', 'string')
                OR (pg_catalog.jsonb_typeof(v_payload->'description') = 'string'
                    AND pg_catalog.char_length(v_payload->>'description') > 500)
            )) THEN
                RETURN aap_identity_groups_v1._failure('invalid_request');
            END IF;
            v_name_present := v_payload ? 'name'
                AND pg_catalog.jsonb_typeof(v_payload->'name') = 'string';
            v_description_present := v_payload ? 'description'
                AND pg_catalog.jsonb_typeof(v_payload->'description') = 'string';
            IF v_name_present THEN
                v_new_name := aap_identity_groups_v1._strip_unicode_space(v_payload->>'name');
            END IF;
            IF v_description_present THEN
                v_description := NULLIF(v_payload->>'description', '');
            END IF;
        END IF;
        BEGIN
            v_group_id := (v_payload->>'group_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END;
    ELSIF v_operation = 'assign_user_group' THEN
        IF v_payload_count <> 2 OR NOT (v_payload ?& ARRAY['user_id', 'group_id'])
           OR pg_catalog.jsonb_typeof(v_payload->'user_id') IS DISTINCT FROM 'string'
           OR (v_payload->>'user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_payload->'group_id') NOT IN ('null', 'string')
           OR (pg_catalog.jsonb_typeof(v_payload->'group_id') = 'string'
               AND (v_payload->>'group_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END IF;
        BEGIN
            v_user_id := (v_payload->>'user_id')::uuid;
            v_group_id := CASE WHEN pg_catalog.jsonb_typeof(v_payload->'group_id') = 'null'
                THEN NULL ELSE (v_payload->>'group_id')::uuid END;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END;
    ELSIF v_operation IN ('preview_group_assignments', 'apply_group_assignments') THEN
        IF v_payload_count <> 2 OR NOT (v_payload ?& ARRAY['user_ids', 'group_id'])
           OR pg_catalog.jsonb_typeof(v_payload->'user_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(v_payload->'user_ids') NOT BETWEEN 1 AND 500
           OR pg_catalog.jsonb_typeof(v_payload->'group_id') NOT IN ('null', 'string')
           OR (pg_catalog.jsonb_typeof(v_payload->'group_id') = 'string'
               AND (v_payload->>'group_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_array_elements(v_payload->'user_ids') AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                  OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           ) THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END IF;
        BEGIN
            SELECT pg_catalog.array_agg((item.value #>> '{}')::uuid ORDER BY item.ordinality)
            INTO v_ids
            FROM pg_catalog.jsonb_array_elements(v_payload->'user_ids')
                 WITH ORDINALITY AS item(value, ordinality);
            v_group_id := CASE WHEN pg_catalog.jsonb_typeof(v_payload->'group_id') = 'null'
                THEN NULL ELSE (v_payload->>'group_id')::uuid END;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_identity_groups_v1._failure('invalid_request');
        END;
    END IF;

    IF v_is_write THEN
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-identity-group-operation:' || v_operation_id::text, 0)
        ) THEN
            RETURN aap_identity_groups_v1._failure('group_busy');
        END IF;
        SELECT * INTO v_receipt
        FROM public.identity_group_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        v_has_receipt := FOUND;
    END IF;

    -- Replays authenticate before returning retained data. The receipt itself
    -- is checked before group locks so deletion can be replayed after the row is gone.
    IF v_has_receipt THEN
        v_actor_fence := aap_identity_groups_v1._group_actor(v_actor, v_expected_key_hash);
        IF v_actor_fence->'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_actor_fence;
        END IF;
        v_principal := v_actor_fence->'data';
        v_actor_role := v_principal->>'role';
        IF v_actor_role NOT IN ('super_admin', 'project_admin') THEN
            RETURN aap_identity_groups_v1._failure(
                'policy_rejected', 403, pg_catalog.to_jsonb('当前账号已无管理权限'::text)
            );
        END IF;
        IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt.operation IS DISTINCT FROM v_operation
           OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
           OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
            RETURN aap_identity_groups_v1._failure('operation_conflict');
        END IF;
        IF v_operation = 'assign_user_group' THEN
            BEGIN
                SELECT account.id, account.email, account.is_active, account.role,
                       account.group_id, account.group_name
                INTO v_target FROM public.users AS account
                WHERE account.id = v_user_id AND account.is_active
                FOR NO KEY UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_identity_groups_v1._failure('account_busy');
            END;
            IF NOT FOUND THEN RETURN aap_identity_groups_v1._failure('target_not_found'); END IF;
            BEGIN
                v_scope := aap_identity_groups_v1._group_scope(v_actor_id, ARRAY[v_user_id], true);
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_identity_groups_v1._failure('group_busy');
            END;
            IF NOT (v_user_id = ANY(v_scope)) THEN
                RETURN aap_identity_groups_v1._failure('target_not_found');
            END IF;
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-groups-native/v1', 'ok', true,
            'operation', v_operation, 'operation_id', v_operation_id,
            'principal', v_principal, 'data', v_receipt.result
        );
    END IF;

    -- Mutation lock order is requested Group, actor, then target User rows.
    IF v_operation IN ('update_group', 'delete_group') THEN
        BEGIN
            SELECT * INTO v_group_row FROM public.groups AS group_row
            WHERE group_row.id = v_group_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_groups_v1._failure('group_busy');
        END;
        IF NOT FOUND THEN RETURN aap_identity_groups_v1._failure('group_not_found'); END IF;
    ELSIF v_operation IN ('assign_user_group', 'apply_group_assignments')
       AND v_group_id IS NOT NULL THEN
        BEGIN
            SELECT * INTO v_group_row FROM public.groups AS group_row
            WHERE group_row.id = v_group_id FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_groups_v1._failure('group_busy');
        END;
        IF NOT FOUND THEN RETURN aap_identity_groups_v1._failure('group_not_found'); END IF;
    ELSIF v_operation = 'preview_group_assignments' AND v_group_id IS NOT NULL THEN
        SELECT * INTO v_group_row FROM public.groups AS group_row
        WHERE group_row.id = v_group_id;
        v_missing_group := NOT FOUND;
    END IF;

    v_actor_fence := aap_identity_groups_v1._group_actor(v_actor, v_expected_key_hash);
    IF v_actor_fence->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor_fence;
    END IF;
    v_principal := v_actor_fence->'data';
    v_actor_role := v_principal->>'role';
    IF v_actor_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_identity_groups_v1._failure(
            'policy_rejected', 403, pg_catalog.to_jsonb('当前账号已无管理权限'::text)
        );
    END IF;
    v_audit_actor := pg_catalog.jsonb_build_object(
        'actor_id', v_principal->'actor_id', 'email', v_principal->'email',
        'role', v_principal->'role'
    );

    IF v_operation = 'list_groups' THEN
        SELECT COALESCE(pg_catalog.jsonb_agg(
            aap_identity_groups_v1._group_record(group_row.id)
            ORDER BY group_row.name, group_row.id
        ), '[]'::jsonb)
        INTO v_data FROM public.groups AS group_row;
        v_result := pg_catalog.jsonb_build_object('items', v_data);
    ELSIF v_operation = 'query_groups' THEN
        SELECT pg_catalog.count(*) INTO v_total
        FROM public.groups AS group_row
        WHERE v_search IS NULL OR group_row.name ILIKE ('%' || v_search || '%')
           OR COALESCE(group_row.description, '') ILIKE ('%' || v_search || '%');
        SELECT COALESCE(pg_catalog.jsonb_agg(
            aap_identity_groups_v1._group_record(page_row.id)
            ORDER BY page_row.name, page_row.id
        ), '[]'::jsonb)
        INTO v_items
        FROM (
            SELECT group_row.id, group_row.name
            FROM public.groups AS group_row
            WHERE v_search IS NULL OR group_row.name ILIKE ('%' || v_search || '%')
               OR COALESCE(group_row.description, '') ILIKE ('%' || v_search || '%')
            ORDER BY group_row.name, group_row.id
            OFFSET ((v_page::bigint - 1) * v_page_size) LIMIT v_page_size
        ) AS page_row;
        v_result := pg_catalog.jsonb_build_object(
            'items', COALESCE(v_items, '[]'::jsonb), 'total', v_total,
            'page', v_page, 'page_size', v_page_size,
            'pages', CASE WHEN v_total = 0 THEN 0
                ELSE ((v_total + v_page_size - 1) / v_page_size)::integer END
        );
    ELSIF v_operation = 'create_group' THEN
        IF v_new_name = '' THEN
            RETURN aap_identity_groups_v1._failure(
                'policy_rejected', 400, pg_catalog.to_jsonb('名称不能为空'::text)
            );
        END IF;
        IF EXISTS (SELECT 1 FROM public.groups WHERE name = v_new_name) THEN
            RETURN aap_identity_groups_v1._failure(
                'policy_rejected', 409,
                pg_catalog.to_jsonb(('已存在同名数据组: ' || v_new_name)::text)
            );
        END IF;
        BEGIN
            INSERT INTO public.groups (id, name, description)
            VALUES (v_operation_id, v_new_name, v_description);
        EXCEPTION WHEN unique_violation THEN
            RETURN aap_identity_groups_v1._failure(
                'policy_rejected', 409,
                pg_catalog.to_jsonb(('已存在同名数据组: ' || v_new_name)::text)
            );
        END;
        PERFORM aap_audit_v1.record_identity_group_event(
            'group.create', v_audit_actor, v_context,
            pg_catalog.jsonb_build_object(
                'target_type', 'group', 'target_id', v_operation_id,
                'detail', pg_catalog.jsonb_build_object('name', v_new_name)
            )
        );
        v_result := pg_catalog.jsonb_build_object(
            'group', aap_identity_groups_v1._group_record(v_operation_id)
        );
    ELSIF v_operation = 'update_group' THEN
        IF v_name_present AND v_new_name = '' THEN
            RETURN aap_identity_groups_v1._failure(
                'policy_rejected', 400, pg_catalog.to_jsonb('名称不能为空'::text)
            );
        END IF;
        v_old_name := v_group_row.name;
        IF v_name_present AND v_new_name IS DISTINCT FROM v_old_name
           AND EXISTS (
               SELECT 1 FROM public.groups AS other
               WHERE other.name = v_new_name AND other.id <> v_group_id
           ) THEN
            RETURN aap_identity_groups_v1._failure(
                'policy_rejected', 409,
                pg_catalog.to_jsonb(('已存在同名数据组: ' || (v_payload->>'name'))::text)
            );
        END IF;
        IF v_name_present AND v_new_name IS DISTINCT FROM v_old_name THEN
            v_group_changed := true;
            BEGIN
                PERFORM account.id FROM public.users AS account
                WHERE account.group_id = v_group_id
                ORDER BY account.id FOR NO KEY UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_identity_groups_v1._failure('group_busy');
            END;
            BEGIN
                UPDATE public.groups SET name = v_new_name WHERE id = v_group_id;
                UPDATE public.users SET group_name = v_new_name WHERE group_id = v_group_id;
            EXCEPTION WHEN unique_violation THEN
                RETURN aap_identity_groups_v1._failure(
                    'policy_rejected', 409,
                    pg_catalog.to_jsonb(('已存在同名数据组: ' || (v_payload->>'name'))::text)
                );
            END;
            v_group_row.name := v_new_name;
        END IF;
        IF v_description_present THEN
            v_group_changed := true;
            UPDATE public.groups SET description = v_description WHERE id = v_group_id;
            v_group_row.description := v_description;
        END IF;
        IF v_group_changed THEN
            v_event_detail := pg_catalog.jsonb_build_object(
                'name', CASE WHEN v_name_present AND v_new_name IS DISTINCT FROM v_old_name
                    THEN v_old_name || pg_catalog.chr(8594) || v_new_name
                    ELSE v_group_row.name END
            );
            IF v_description_present THEN
                v_event_detail := v_event_detail || pg_catalog.jsonb_build_object(
                    'description', 'updated'
                );
            END IF;
            PERFORM aap_audit_v1.record_identity_group_event(
                'group.update', v_audit_actor, v_context,
                pg_catalog.jsonb_build_object(
                    'target_type', 'group', 'target_id', v_group_id,
                    'detail', v_event_detail
                )
            );
        END IF;
        v_result := pg_catalog.jsonb_build_object(
            'group', aap_identity_groups_v1._group_record(v_group_id)
        );
    ELSIF v_operation = 'delete_group' THEN
        v_old_name := v_group_row.name;
        BEGIN
            PERFORM account.id FROM public.users AS account
            WHERE account.group_id = v_group_id
            ORDER BY account.id FOR NO KEY UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_groups_v1._failure('group_busy');
        END;
        PERFORM aap_audit_v1.record_identity_group_event(
            'group.delete', v_audit_actor, v_context,
            pg_catalog.jsonb_build_object(
                'target_type', 'group', 'target_id', v_group_id,
                'detail', pg_catalog.jsonb_build_object('name', v_old_name)
            )
        );
        UPDATE public.users SET group_id = NULL, group_name = NULL
        WHERE group_id = v_group_id;
        DELETE FROM public.groups WHERE id = v_group_id;
        v_result := pg_catalog.jsonb_build_object('group_id', v_group_id, 'deleted', true);
    ELSIF v_operation = 'assign_user_group' THEN
        BEGIN
            SELECT account.id, account.email, account.is_active, account.role,
                   account.group_id, account.group_name
            INTO v_target FROM public.users AS account
            WHERE account.id = v_user_id
            FOR NO KEY UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_groups_v1._failure('account_busy');
        END;
        IF NOT FOUND OR NOT v_target.is_active THEN
            RETURN aap_identity_groups_v1._failure('target_not_found');
        END IF;
        BEGIN
            v_scope := aap_identity_groups_v1._group_scope(v_actor_id, ARRAY[v_user_id], true);
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_groups_v1._failure('group_busy');
        END;
        IF NOT (v_user_id = ANY(v_scope)) THEN
            RETURN aap_identity_groups_v1._failure('target_not_found');
        END IF;
        v_old_group_id := v_target.group_id;
        v_old_group_name := v_target.group_name;
        v_new_group_name := CASE WHEN v_group_id IS NULL THEN NULL ELSE v_group_row.name END;
        UPDATE public.users
        SET group_id = v_group_id, group_name = v_new_group_name
        WHERE id = v_user_id;
        PERFORM aap_audit_v1.record_identity_group_event(
            'user.group_change', v_audit_actor, v_context,
            pg_catalog.jsonb_build_object(
                'target_type', 'user', 'target_id', v_user_id,
                'detail', pg_catalog.jsonb_build_object(
                    'email', v_target.email,
                    'old_group_id', v_old_group_id,
                    'old_group_name', v_old_group_name,
                    'new_group_id', v_group_id,
                    'new_group_name', v_new_group_name
                )
            )
        );
        v_result := pg_catalog.jsonb_build_object(
            'account', aap_identity_groups_v1._account_record(v_user_id)
        );
    ELSIF v_operation = 'preview_group_assignments' THEN
        BEGIN
            v_allowed := aap_identity_groups_v1._group_scope(v_actor_id, v_ids, false);
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_groups_v1._failure('group_busy');
        END;
        v_items := '[]'::jsonb;
        v_succeeded := 0;
        v_failed := 0;
        FOR v_index IN 1..pg_catalog.cardinality(v_ids) LOOP
            v_user_id := v_ids[v_index];
            SELECT account.id, account.email, account.name, account.is_active,
                   account.role, account.group_id, account.group_name
            INTO v_target FROM public.users AS account
            WHERE account.id = v_user_id;
            v_in_scope := FOUND AND v_user_id = ANY(v_allowed);
            IF NOT v_in_scope THEN
                v_item_error := '用户不存在或不在管理范围内';
                v_failed := v_failed + 1;
                v_item := pg_catalog.jsonb_build_object(
                    'user_id', v_user_id, 'email', NULL, 'name', NULL, 'ok', false,
                    'current_group_id', NULL, 'current_group_name', NULL,
                    'next_group_id', NULL, 'next_group_name', NULL, 'error', v_item_error
                );
            ELSIF v_missing_group THEN
                v_item_error := '数据组不存在';
                v_failed := v_failed + 1;
                v_item := pg_catalog.jsonb_build_object(
                    'user_id', v_user_id, 'email', v_target.email, 'name', v_target.name,
                    'ok', false, 'current_group_id', NULL,
                    'current_group_name', NULL,
                    'next_group_id', NULL, 'next_group_name', NULL, 'error', v_item_error
                );
            ELSE
                v_succeeded := v_succeeded + 1;
                v_item := pg_catalog.jsonb_build_object(
                    'user_id', v_user_id, 'email', v_target.email, 'name', v_target.name,
                    'ok', true, 'current_group_id', v_target.group_id,
                    'current_group_name', v_target.group_name,
                    'next_group_id', v_group_id,
                    'next_group_name', CASE WHEN v_group_id IS NULL THEN NULL ELSE v_group_row.name END,
                    'error', NULL
                );
            END IF;
            v_items := v_items || pg_catalog.jsonb_build_array(v_item);
        END LOOP;
        v_result := pg_catalog.jsonb_build_object(
            'group_id', v_group_id,
            'group_name', CASE WHEN v_missing_group OR v_succeeded = 0 THEN NULL
                WHEN v_group_id IS NULL THEN NULL ELSE v_group_row.name END,
            'items', v_items, 'applicable', v_succeeded, 'blocked', v_failed
        );
    ELSIF v_operation = 'apply_group_assignments' THEN
        v_items := '[]'::jsonb;
        v_succeeded := 0;
        v_failed := 0;
        FOR v_index IN 1..pg_catalog.cardinality(v_ids) LOOP
            v_user_id := v_ids[v_index];
            v_item_error := NULL;
            BEGIN
                SELECT account.id, account.email, account.is_active, account.role,
                       account.group_id, account.group_name
                INTO v_target FROM public.users AS account
                WHERE account.id = v_user_id
                FOR NO KEY UPDATE NOWAIT;
                IF NOT FOUND THEN
                    v_item_error := '用户不存在或不在管理范围内';
                ELSE
                    v_scope := aap_identity_groups_v1._group_scope(
                        v_actor_id, ARRAY[v_user_id], true
                    );
                    IF NOT (v_user_id = ANY(v_scope)) THEN
                        v_item_error := '用户不存在或不在管理范围内';
                    ELSE
                        v_old_group_id := v_target.group_id;
                        v_old_group_name := v_target.group_name;
                        v_new_group_name := CASE WHEN v_group_id IS NULL
                            THEN NULL ELSE v_group_row.name END;
                        UPDATE public.users
                        SET group_id = v_group_id, group_name = v_new_group_name
                        WHERE id = v_user_id;
                        PERFORM aap_audit_v1.record_identity_group_event(
                            'user.bulk_group_assign', v_audit_actor, v_context,
                            pg_catalog.jsonb_build_object(
                                'target_type', 'user', 'target_id', v_user_id,
                                'detail', pg_catalog.jsonb_build_object(
                                    'group_id', v_group_id, 'group_name', v_new_group_name
                                )
                            )
                        );
                        v_succeeded := v_succeeded + 1;
                    END IF;
                END IF;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                v_item_error := '处理失败，请稍后重试';
            END;
            IF v_item_error IS NULL THEN
                v_item := pg_catalog.jsonb_build_object(
                    'user_id', v_user_id, 'ok', true, 'retryable', false, 'error', NULL
                );
            ELSE
                v_failed := v_failed + 1;
                v_item := pg_catalog.jsonb_build_object(
                    'user_id', v_user_id, 'ok', false, 'retryable', true,
                    'error', v_item_error
                );
            END IF;
            v_items := v_items || pg_catalog.jsonb_build_array(v_item);
        END LOOP;
        v_result := pg_catalog.jsonb_build_object(
            'items', v_items, 'succeeded', v_succeeded, 'failed', v_failed
        );
    END IF;

    IF v_is_write THEN
        INSERT INTO public.identity_group_operation_receipts (
            operation_id, actor_id, operation, request_fingerprint, derivation_id, result
        ) VALUES (
            v_operation_id, v_actor_id, v_operation, v_fingerprint, v_derivation_id, v_result
        ) ON CONFLICT (operation_id) DO NOTHING;
        IF NOT FOUND THEN
            RETURN aap_identity_groups_v1._failure('operation_conflict');
        END IF;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-groups-native/v1', 'ok', true,
        'operation', v_operation, 'operation_id', v_operation_id,
        'principal', v_principal, 'data', v_result
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_groups_v1.execute_group_command(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_groups_v1.resolve_invitation_group(
    p_invitation_id uuid,
    p_token text
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
    v_invitation record;
    v_inviter_role text;
    v_inviter_active boolean;
    v_name text;
    v_group_id uuid;
BEGIN
    IF p_invitation_id IS NULL OR p_token IS NULL
       OR pg_catalog.octet_length(p_token) NOT BETWEEN 1 AND 64 THEN
        RETURN pg_catalog.jsonb_build_object(
            'error', pg_catalog.jsonb_build_object('code', 'invitation_invalid')
        );
    END IF;
    BEGIN
        SELECT invitation.id, invitation.invited_by, invitation.role,
               invitation.group_name
        INTO v_invitation
        FROM public.user_invitations AS invitation
        WHERE invitation.id = p_invitation_id
          AND invitation.token = p_token
          AND invitation.accepted_at IS NULL
          AND invitation.revoked_at IS NULL
          AND invitation.expires_at > pg_catalog.clock_timestamp()
        FOR SHARE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RAISE;
    END;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'error', pg_catalog.jsonb_build_object('code', 'invitation_invalid')
        );
    END IF;

    BEGIN
        SELECT inviter.role, inviter.is_active
        INTO v_inviter_role, v_inviter_active
        FROM public.users AS inviter
        WHERE inviter.id = v_invitation.invited_by
        FOR KEY SHARE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RAISE;
    END;
    IF NOT FOUND OR NOT v_inviter_active
       OR v_inviter_role NOT IN ('super_admin', 'project_admin')
       OR (v_invitation.role IN ('super_admin', 'project_admin')
           AND v_inviter_role <> 'super_admin') THEN
        RETURN pg_catalog.jsonb_build_object(
            'error', pg_catalog.jsonb_build_object('code', 'invitation_invalid')
        );
    END IF;

    v_name := aap_identity_groups_v1._strip_unicode_space(v_invitation.group_name);
    IF v_name IS NULL OR v_name = '' THEN
        RETURN NULL;
    END IF;
    IF pg_catalog.char_length(v_name) > 100 THEN
        RETURN pg_catalog.jsonb_build_object(
            'error', pg_catalog.jsonb_build_object('code', 'invalid_group_name')
        );
    END IF;

    SELECT group_row.id INTO v_group_id
    FROM public.groups AS group_row
    WHERE group_row.name = v_name
    FOR SHARE;
    IF NOT FOUND THEN
        INSERT INTO public.groups (name)
        VALUES (v_name)
        ON CONFLICT (name) DO NOTHING;
        SELECT group_row.id INTO v_group_id
        FROM public.groups AS group_row
        WHERE group_row.name = v_name
        FOR SHARE;
    END IF;
    IF v_group_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invitation group resolution failed';
    END IF;
    RETURN pg_catalog.jsonb_build_object('id', v_group_id, 'name', v_name);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_groups_v1.resolve_invitation_group(uuid, text)
    FROM PUBLIC;
