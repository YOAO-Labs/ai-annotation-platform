-- Final authorization check for an API key whose credential authentication
-- already succeeded in the caller-owned training execution.
CREATE FUNCTION aap_identity_v1.check_accepted_api_key(
    p_actor_id uuid,
    p_api_key_id uuid,
    p_required_scopes text[]
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_key_actor_id uuid;
    v_revoked_at timestamptz;
    v_expires_at timestamptz;
    v_scopes jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_api_key_id IS NULL OR p_required_scopes IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid_request';
    END IF;
    IF COALESCE(pg_catalog.array_ndims(p_required_scopes),0)>1 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid_request';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(p_required_scopes) AS requested(scope)
        WHERE requested.scope IS NULL
    ) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid_request';
    END IF;

    -- SHARE waits for a concurrent revoke/update and then confirms the row
    -- version that won the lock. The caller already holds User/Project locks.
    SELECT key.user_id,key.revoked_at,key.expires_at,key.scopes
    INTO v_key_actor_id,v_revoked_at,v_expires_at,v_scopes
    FROM public.api_keys AS key
    WHERE key.id=p_api_key_id
    FOR SHARE;
    IF NOT FOUND OR v_key_actor_id IS DISTINCT FROM p_actor_id
       OR v_revoked_at IS NOT NULL
       OR (v_expires_at IS NOT NULL
           AND v_expires_at <= pg_catalog.clock_timestamp()) THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(v_scopes) IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(v_scopes) AS stored(value)
        WHERE pg_catalog.jsonb_typeof(stored.value) IS DISTINCT FROM 'string'
    ) THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(v_scopes) AS stored(value)
        WHERE stored.value #>> '{}'='*'
    ) THEN
        RETURN true;
    END IF;
    RETURN NOT EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(p_required_scopes) AS requested(scope)
        WHERE NOT EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_scopes) AS stored(value)
            WHERE stored.value #>> '{}'=requested.scope
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_v1.check_accepted_api_key(uuid,uuid,text[]) FROM PUBLIC;
