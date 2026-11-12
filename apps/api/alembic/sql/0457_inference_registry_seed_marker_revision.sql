-- The screenshot-seed revision advances with the seed content, so the ingress
-- marker must validate the seed namespace and the exact managed_by/profile
-- identity instead of one frozen revision literal. The marker still owns only
-- the `seed` key; a new revision replaces that key and preserves caller edits.
CREATE OR REPLACE FUNCTION
    aap_inference_registry_ingress_v1.tag_seed_registry_plugin_legacy(
        p_registry_id uuid,
        p_marker_json text
    )
RETURNS TABLE (
    extra_params jsonb,
    updated_at timestamptz
)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_marker jsonb;
    v_current jsonb;
    v_extra jsonb;
    v_merged jsonb;
    v_current_updated_at timestamptz;
    v_updated_at timestamptz;
BEGIN
    IF p_registry_id IS NULL OR p_marker_json IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry seed marker';
    END IF;

    BEGIN
        v_marker := p_marker_json::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry seed marker';
    END;

    IF pg_catalog.jsonb_typeof(v_marker) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry seed marker';
    END IF;

    IF (
        SELECT pg_catalog.count(*)
        FROM pg_catalog.jsonb_object_keys(v_marker)
    ) <> 3
       OR NOT (v_marker ?& ARRAY['managed_by', 'profile', 'revision'])
       OR v_marker ->> 'managed_by' IS DISTINCT FROM 'screenshot-seed'
       OR v_marker ->> 'profile' IS DISTINCT FROM 'screenshots'
       OR COALESCE(v_marker ->> 'revision', '')
          !~ '^screenshots-[0-9]{4}-[0-9]{2}-[a-z]+$' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry seed marker';
    END IF;

    SELECT registry.extra_params,
           registry.updated_at
    INTO v_current, v_current_updated_at
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR UPDATE OF registry;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'registry ingress seed row changed';
    END IF;

    IF v_current IS NULL
       OR v_current = 'null'::jsonb
       OR v_current = 'false'::jsonb
       OR v_current = '0'::jsonb
       OR v_current = '""'::jsonb
       OR v_current = '[]'::jsonb THEN
        v_extra := '{}'::jsonb;
    ELSIF pg_catalog.jsonb_typeof(v_current) = 'object' THEN
        v_extra := v_current;
    ELSE
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry seed extra parameters';
    END IF;

    v_merged := v_extra || pg_catalog.jsonb_build_object('seed', v_marker);
    IF v_merged IS DISTINCT FROM v_current THEN
        UPDATE public.ml_backend_registry AS registry
        SET extra_params = v_merged,
            updated_at = pg_catalog.now()
        WHERE registry.id = p_registry_id
        RETURNING registry.updated_at INTO v_updated_at;
        extra_params := v_merged;
        updated_at := v_updated_at;
    ELSE
        extra_params := v_current;
        updated_at := v_current_updated_at;
    END IF;

    RETURN NEXT;
END;
$function$;
