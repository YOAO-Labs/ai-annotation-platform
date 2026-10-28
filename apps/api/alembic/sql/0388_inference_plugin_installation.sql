-- A caller-owned transaction retains the Registry row lock until commit.  The
-- returned facts deliberately exclude the endpoint credential.
CREATE FUNCTION aap_inference_topology_v1.lock_plugin_registry(p_registry_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_registry_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid registry id';
    END IF;
    PERFORM 1 FROM public.ml_backend_registry WHERE id = p_registry_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    RETURN aap_inference_topology_v1._registry_json(p_registry_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.lock_plugin_registry(uuid) FROM PUBLIC;
