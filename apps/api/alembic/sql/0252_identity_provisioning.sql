-- Narrow operational account lookup/create capabilities; callers keep their
-- database transaction, and an email uniqueness error is intentionally raw.
CREATE FUNCTION aap_identity_v1.read_provisioning_account(p_email text)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_account_id uuid;
BEGIN
    IF p_email IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    SELECT account.id
    INTO v_account_id
    FROM public.users AS account
    WHERE account.email = p_email;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-provisioning/v1',
        'email', p_email,
        'account_id', v_account_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.read_provisioning_account(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.create_provisioning_account(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_field_count integer;
    v_mode text;
    v_account_id uuid;
    v_email text;
    v_name text;
    v_password_hash text;
    v_now timestamptz;
    v_request_id text;
    v_audit_id bigint;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_field_count <> 8
       OR NOT (p_request ?& ARRAY[
           'schema', 'mode', 'account_id', 'email', 'name', 'password_hash',
           'now', 'request_id'
       ])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
           WHERE field.name NOT IN (
               'schema', 'mode', 'account_id', 'email', 'name', 'password_hash',
               'now', 'request_id'
           )
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    IF pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' IS DISTINCT FROM 'aap-identity-provisioning/v1'
       OR pg_catalog.jsonb_typeof(p_request->'mode') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'account_id') IS DISTINCT FROM 'string'
       OR (p_request->>'account_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'name') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'password_hash') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'now') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(p_request->'request_id') NOT IN ('string', 'null') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    v_mode := p_request->>'mode';
    v_email := p_request->>'email';
    v_name := p_request->>'name';
    v_password_hash := p_request->>'password_hash';
    v_request_id := p_request->>'request_id';
    BEGIN
        v_account_id := (p_request->>'account_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END;

    IF v_mode = 'bootstrap_admin' THEN
        IF pg_catalog.jsonb_typeof(p_request->'now') IS DISTINCT FROM 'string'
           OR (p_request->>'now') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
        END IF;
        BEGIN
            v_now := (p_request->>'now')::timestamptz;
        EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow
            OR invalid_time_zone_displacement_value THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
        END;
    ELSIF v_mode = 'import_admin' THEN
        IF v_email IS DISTINCT FROM 'admin'
           OR v_name IS DISTINCT FROM '超级管理员'
           OR pg_catalog.jsonb_typeof(p_request->'now') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_request->'request_id') IS DISTINCT FROM 'null' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
        END IF;
    ELSE
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    IF v_mode = 'bootstrap_admin' THEN
        INSERT INTO public.users (
            id, email, name, password_hash, credential_generation, role,
            group_name, group_id, status, is_active, email_verified_at,
            preferences
        ) VALUES (
            v_account_id, v_email, v_name, v_password_hash, 0, 'super_admin',
            NULL, NULL, 'online', true, v_now, '{}'::jsonb
        );
        v_audit_id := aap_audit_v1.record_identity_bootstrap_admin(
            v_account_id, v_email, v_request_id
        );
        IF v_audit_id IS NULL OR v_audit_id <= 0 THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid bootstrap Audit result';
        END IF;
    ELSE
        INSERT INTO public.users (
            id, email, name, password_hash, credential_generation, role,
            group_name, group_id, status, is_active, email_verified_at,
            preferences
        ) VALUES (
            v_account_id, 'admin', '超级管理员', v_password_hash, 0,
            'super_admin', NULL, NULL, 'offline', true, NULL, '{}'::jsonb
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-provisioning/v1',
        'email', v_email,
        'account_id', v_account_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.create_provisioning_account(jsonb)
    FROM PUBLIC;
