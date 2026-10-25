CREATE OR REPLACE FUNCTION aap_inference_v1.ai_mask_pool_reference_row(
    p_pool_id uuid
)
RETURNS TABLE (
    id uuid,
    name text,
    enabled boolean,
    routing_policy text,
    legacy_instance_id uuid,
    routing_generation bigint,
    capability_fingerprint text,
    capability_snapshot jsonb,
    created_at timestamptz,
    updated_at timestamptz
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
    SELECT pool.id,
           pool.name::text,
           pool.enabled,
           pool.routing_policy::text,
           pool.legacy_instance_id,
           pool.routing_generation,
           pool.capability_fingerprint::text,
           pool.capability_snapshot,
           pool.created_at,
           pool.updated_at
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_v1.ai_mask_pool_reference_row(uuid)
    FROM PUBLIC;
