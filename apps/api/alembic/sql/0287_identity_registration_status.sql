-- Expose the effective open-registration setting through the Recovery owner.
CREATE FUNCTION aap_identity_recovery_v1.read_registration_status(
    p_fallbacks jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_policy jsonb;
    v_field_count integer;
BEGIN
    v_policy := aap_settings_v1.identity_recovery_policy(p_fallbacks);
    IF pg_catalog.jsonb_typeof(v_policy) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023', MESSAGE = 'invalid registration policy';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(v_policy);
    IF v_field_count <> 2
       OR NOT (v_policy ?& ARRAY['allow_open_registration', 'frontend_base_url'])
       OR pg_catalog.jsonb_typeof(v_policy->'allow_open_registration') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(
           v_policy->'frontend_base_url'
       ) IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023', MESSAGE = 'invalid registration policy';
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-registration-status/v1',
        'operation', 'read_status',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'allow_open_registration', v_policy->'allow_open_registration'
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_recovery_v1.read_registration_status(jsonb) FROM PUBLIC;
