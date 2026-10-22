CREATE FUNCTION aap_identity_v1.replace_fixture_preferences(
    p_user_id uuid,
    p_preferences jsonb
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
    v_database_name text := pg_catalog.lower(pg_catalog.current_database());
    v_row_count integer;
BEGIN
    IF pg_catalog.right(v_database_name, 5) <> '_test'
       AND pg_catalog.right(v_database_name, 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'fixture preference replacement requires a test database';
    END IF;

    IF p_preferences IS NULL
       OR pg_catalog.jsonb_typeof(p_preferences) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'fixture preferences must be a JSON object';
    END IF;

    UPDATE public.users
    SET preferences = p_preferences
    WHERE id = p_user_id;
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_v1.replace_fixture_preferences(uuid, jsonb)
    FROM PUBLIC;
