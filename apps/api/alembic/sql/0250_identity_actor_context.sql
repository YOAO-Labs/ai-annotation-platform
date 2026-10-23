-- Read the current admitted account context without changing authentication state.
CREATE FUNCTION aap_identity_v1.read_actor_context(
    p_actor_id uuid,
    p_admitted_role text
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor_id uuid;
    v_name text;
    v_email text;
    v_role text;
    v_is_active boolean;
    v_weekly_target_default integer;
BEGIN
    IF p_actor_id IS NULL OR p_admitted_role IS NULL
       OR p_admitted_role NOT IN (
           'super_admin', 'project_admin', 'employee', 'viewer'
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-actor/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    SELECT account.id, account.name, account.email, account.role,
           account.is_active, account.weekly_target_default
    INTO v_actor_id, v_name, v_email, v_role, v_is_active,
         v_weekly_target_default
    FROM public.users AS account
    WHERE account.id = p_actor_id;

    IF NOT FOUND
       OR v_is_active IS DISTINCT FROM TRUE
       OR v_role IS DISTINCT FROM p_admitted_role THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-actor/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'credential_invalid')
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-actor/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'id', v_actor_id,
            'name', v_name,
            'email', v_email,
            'role', v_role,
            'is_active', TRUE,
            'weekly_target_default', v_weekly_target_default
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.read_actor_context(uuid, text)
    FROM PUBLIC;
