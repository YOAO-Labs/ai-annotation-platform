CREATE FUNCTION aap_identity_v1.create_seed_persona_account(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_field_count integer;
    v_account_id uuid;
    v_email text;
    v_name text;
    v_password_hash text;
    v_role text;
    v_group_name text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_field_count <> 7
       OR NOT (p_request ?& ARRAY[
           'schema', 'account_id', 'email', 'name', 'password_hash', 'role',
           'group_name'
       ])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
           WHERE field.name NOT IN (
               'schema', 'account_id', 'email', 'name', 'password_hash', 'role',
               'group_name'
           )
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    IF pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' IS DISTINCT FROM 'aap-identity-seed-persona/v1'
       OR pg_catalog.jsonb_typeof(p_request->'account_id') IS DISTINCT FROM 'string'
       OR (p_request->>'account_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'name') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'password_hash') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'role') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'group_name') NOT IN ('string', 'null') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    v_email := p_request->>'email';
    v_name := p_request->>'name';
    v_password_hash := p_request->>'password_hash';
    v_role := p_request->>'role';
    v_group_name := p_request->>'group_name';
    BEGIN
        v_account_id := (p_request->>'account_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END;

    IF v_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    INSERT INTO public.users (
        id, email, name, password_hash, credential_generation, role,
        group_name, group_id, status, is_active, disabled_kind, disabled_at,
        disabled_by, disabled_reason, email_verified_at, last_login_at,
        last_seen_at, password_admin_reset_at, deactivation_requested_at,
        deactivation_reason, deactivation_scheduled_at, weekly_target_default,
        avatar_ref, preferences
    ) VALUES (
        v_account_id, v_email, v_name, v_password_hash, 0, v_role,
        v_group_name, NULL, 'offline', true, NULL, NULL, NULL, NULL, NULL,
        NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, '{}'::jsonb
    );

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-seed-persona/v1',
        'email', v_email,
        'account_id', v_account_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_v1.create_seed_persona_account(jsonb)
    FROM PUBLIC;
