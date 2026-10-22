CREATE SCHEMA aap_identity_fixture_v1;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_fixture_v1 FROM PUBLIC;
-- aap-statement-break
CREATE VIEW aap_identity_fixture_v1.account_profiles
WITH (security_barrier = true)
AS
SELECT
    account.id,
    account.email,
    account.name,
    account.role,
    account.group_name,
    account.group_id,
    account.status,
    account.is_active,
    account.disabled_kind,
    account.disabled_at,
    account.disabled_by,
    account.disabled_reason,
    account.email_verified_at,
    account.last_login_at,
    account.last_seen_at,
    account.password_admin_reset_at,
    account.deactivation_requested_at,
    account.deactivation_scheduled_at,
    account.preferences,
    account.avatar_ref,
    account.created_at
FROM public.users AS account
WHERE pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) = '_test'
   OR pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) = '_e2e';
-- aap-statement-break
REVOKE ALL ON TABLE aap_identity_fixture_v1.account_profiles FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_fixture_v1.create_account(
    p_id uuid,
    p_email text,
    p_name text,
    p_password_hash text,
    p_role text
)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_account_id uuid;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;

    INSERT INTO public.users (
        id, email, name, password_hash, credential_generation, role,
        group_name, group_id, status, is_active, disabled_kind, disabled_at,
        disabled_by, disabled_reason, email_verified_at, last_login_at,
        last_seen_at, password_admin_reset_at, deactivation_requested_at,
        deactivation_reason, deactivation_scheduled_at, weekly_target_default,
        avatar_ref, preferences
    ) VALUES (
        p_id, p_email, p_name, p_password_hash, 0, p_role,
        NULL, NULL, 'offline', true, NULL, NULL,
        NULL, NULL, NULL, NULL,
        NULL, NULL, NULL,
        NULL, NULL, NULL,
        NULL, '{}'::jsonb
    )
    RETURNING id INTO v_account_id;
    RETURN v_account_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_fixture_v1.create_account(uuid, text, text, text, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_fixture_v1.mark_manual_inactive(
    p_id uuid,
    p_disabled_at timestamptz,
    p_disabled_by uuid
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_row_count integer;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;

    UPDATE public.users
    SET is_active = false,
        status = 'offline',
        disabled_kind = 'manual',
        disabled_at = p_disabled_at,
        disabled_by = p_disabled_by
    WHERE id = p_id;
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_fixture_v1.mark_manual_inactive(uuid, timestamptz, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_fixture_v1.create_invitations(p_invitations jsonb)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_row_count integer;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;

    IF p_invitations IS NULL
       OR pg_catalog.jsonb_typeof(p_invitations) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid E2E Invitation records';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(p_invitations) AS item(value)
        WHERE CASE
            WHEN pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'object' THEN true
            ELSE
                NOT (item.value ?& ARRAY[
                    'id', 'email', 'role', 'token', 'expires_at', 'invited_by'
                ])
                OR EXISTS (
                    SELECT 1
                    FROM pg_catalog.jsonb_object_keys(item.value) AS field(name)
                    WHERE field.name <> ALL (ARRAY[
                        'id', 'email', 'role', 'project_role', 'group_name',
                        'project_id', 'token', 'expires_at', 'invited_by',
                        'accepted_at', 'accepted_user_id', 'revoked_at'
                    ])
                )
                OR pg_catalog.jsonb_typeof(item.value -> 'id') IS DISTINCT FROM 'string'
                OR pg_catalog.jsonb_typeof(item.value -> 'email') IS DISTINCT FROM 'string'
                OR pg_catalog.jsonb_typeof(item.value -> 'role') IS DISTINCT FROM 'string'
                OR pg_catalog.jsonb_typeof(item.value -> 'token') IS DISTINCT FROM 'string'
                OR pg_catalog.jsonb_typeof(item.value -> 'expires_at') IS DISTINCT FROM 'string'
                OR pg_catalog.jsonb_typeof(item.value -> 'invited_by') IS DISTINCT FROM 'string'
                OR (
                    item.value ? 'project_role'
                    AND pg_catalog.jsonb_typeof(item.value -> 'project_role') NOT IN ('string', 'null')
                )
                OR (
                    item.value ? 'group_name'
                    AND pg_catalog.jsonb_typeof(item.value -> 'group_name') NOT IN ('string', 'null')
                )
                OR (
                    item.value ? 'project_id'
                    AND pg_catalog.jsonb_typeof(item.value -> 'project_id') NOT IN ('string', 'null')
                )
                OR (
                    item.value ? 'accepted_at'
                    AND pg_catalog.jsonb_typeof(item.value -> 'accepted_at') NOT IN ('string', 'null')
                )
                OR (
                    item.value ? 'accepted_user_id'
                    AND pg_catalog.jsonb_typeof(item.value -> 'accepted_user_id') NOT IN ('string', 'null')
                )
                OR (
                    item.value ? 'revoked_at'
                    AND pg_catalog.jsonb_typeof(item.value -> 'revoked_at') NOT IN ('string', 'null')
                )
        END
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid E2E Invitation records';
    END IF;

    INSERT INTO public.user_invitations (
        id, email, role, project_role, group_name, project_id, token,
        expires_at, invited_by, accepted_at, accepted_user_id, revoked_at
    )
    SELECT
        invitation.id, invitation.email, invitation.role, invitation.project_role,
        invitation.group_name, invitation.project_id, invitation.token,
        invitation.expires_at, invitation.invited_by, invitation.accepted_at,
        invitation.accepted_user_id, invitation.revoked_at
    FROM pg_catalog.jsonb_to_recordset(p_invitations) AS invitation (
        id uuid,
        email text,
        role text,
        project_role text,
        group_name text,
        project_id uuid,
        token text,
        expires_at timestamptz,
        invited_by uuid,
        accepted_at timestamptz,
        accepted_user_id uuid,
        revoked_at timestamptz
    );
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_fixture_v1.create_invitations(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_fixture_v1.delete_accounts(
    p_shared boolean,
    p_emails text[]
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_row_count integer;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;

    IF p_shared IS TRUE THEN
        DELETE FROM public.users
        WHERE email LIKE '%@e2e.test';
    ELSE
        DELETE FROM public.users
        WHERE email = ANY (p_emails);
    END IF;
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_fixture_v1.delete_accounts(boolean, text[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_fixture_v1.delete_reset_tokens(p_user_ids uuid[])
RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_row_count integer;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;

    DELETE FROM public.password_reset_tokens
    WHERE user_id = ANY (p_user_ids);
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_fixture_v1.delete_reset_tokens(uuid[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_fixture_v1.delete_invitations(p_inviter_ids uuid[])
RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_row_count integer;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;

    DELETE FROM public.user_invitations
    WHERE invited_by = ANY (p_inviter_ids);
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_fixture_v1.delete_invitations(uuid[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_fixture_v1.delete_organization_members(
    p_user_ids uuid[]
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_row_count integer;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;

    DELETE FROM public.organization_members
    WHERE user_id = ANY (p_user_ids);
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_fixture_v1.delete_organization_members(uuid[])
    FROM PUBLIC;
