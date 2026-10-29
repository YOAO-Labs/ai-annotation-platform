-- Restore the preceding endpoint projection.
CREATE OR REPLACE FUNCTION aap_inference_backend_endpoint_v1._endpoint_transport_identity(p_endpoint jsonb)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'url', p_endpoint -> 'url',
        'auth_method', p_endpoint -> 'auth_method',
        'auth_token', p_endpoint -> 'auth_token',
        'auth_revision', p_endpoint -> 'auth_revision',
        'gpu_resource_id', p_endpoint -> 'gpu_resource_id',
        'extra_params', p_endpoint -> 'extra_params'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_backend_endpoint_v1._endpoint_transport_identity(jsonb) FROM PUBLIC;
