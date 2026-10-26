-- Test-only health state for the existing, explicitly marked workbench mock.
CREATE FUNCTION aap_inference_topology_v1.seed_workbench_health(p_registry_id uuid)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE = '42501',
            MESSAGE = 'Inference workbench fixture writes require a test database';
    END IF;
    UPDATE public.ml_backend_registry AS registry
    SET state = 'connected', updated_at = pg_catalog.clock_timestamp()
    WHERE registry.id = p_registry_id
      AND registry.auth_method = 'none'
      AND registry.auth_token IS NULL
      AND registry.gpu_resource_id IS NULL
      AND registry.vram_budget_mb IS NULL
      AND registry.source = 'manual'
      AND registry.extra_params = '{"e2e_mock": true}'::jsonb;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Inference workbench mock identity mismatch';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.seed_workbench_health(uuid) FROM PUBLIC;
