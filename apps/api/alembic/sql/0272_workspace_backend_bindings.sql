-- Clear only the deleted backend pool's Project bindings in the caller transaction.
CREATE FUNCTION aap_workspace_v1.clear_primary_backend_bindings(
    p_pool_id uuid
)
RETURNS bigint
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
    v_rows_cleared bigint;
BEGIN
    IF p_pool_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace backend binding request';
    END IF;

    UPDATE public.projects AS project
    SET ml_backend_pool_id = NULL,
        updated_at = pg_catalog.now()
    WHERE project.ml_backend_pool_id = p_pool_id;

    GET DIAGNOSTICS v_rows_cleared = ROW_COUNT;
    RETURN v_rows_cleared;
END;
$function$;
-- aap-statement-break
ALTER FUNCTION aap_workspace_v1.clear_primary_backend_bindings(uuid)
    OWNER TO workspace_code;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.clear_primary_backend_bindings(uuid)
    FROM PUBLIC;
-- aap-statement-break
COMMENT ON FUNCTION aap_workspace_v1.clear_primary_backend_bindings(uuid)
    IS 'Clears Project primary backend-pool bindings for one exact pool in the caller transaction.';
