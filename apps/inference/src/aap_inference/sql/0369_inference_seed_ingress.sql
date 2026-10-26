-- Public screenshot-seed ingress never exports stored Registry credentials.
CREATE FUNCTION aap_inference_topology_v1.seed_registry_by_url(p_url text)
RETURNS TABLE (
    id uuid, name text, url text, state text, is_interactive boolean,
    source text, health_meta jsonb, extra_params jsonb
)
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT registry.id, registry.name, registry.url, registry.state,
           registry.is_interactive, registry.source,
           registry.health_meta, registry.extra_params
    FROM aap_inference_registry_ingress_v1.registry_by_url(p_url) AS registry
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.seed_registry_by_url(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.create_seed_registry(
    p_registry_id uuid, p_name text, p_url text, p_marker text
)
RETURNS TABLE (
    id uuid, name text, url text, state text, is_interactive boolean,
    source text, health_meta jsonb, extra_params jsonb
)
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT registry.id, registry.name, registry.url, registry.state,
           registry.is_interactive, registry.source,
           registry.health_meta, registry.extra_params
    FROM aap_inference_registry_ingress_v1.create_registry(
        p_registry_id, p_name, p_url, 'seed', false,
        pg_catalog.jsonb_build_object('seed', p_marker::jsonb)::text
    ) AS registry
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.create_seed_registry(uuid,text,text,text) FROM PUBLIC;
